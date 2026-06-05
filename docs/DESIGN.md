# Lotsman — Design

> In-image MCP-сервер для compute-тулов. Знает свой образ, ловит локальные
> ловушки, отдаёт чистый API наверх.

**Статус:** заготовка после первого design-разговора (2026-05-05).
Уточнения и решения накапливать здесь же или в `docs/DECISIONS.md`.

---

## Имя

**Lotsman / Лоцман.** Местный лоцман заходит на чужой корабль и проводит его
через свои воды. Каждый образ имеет своего Лоцмана, который знает локальные
ловушки (MSYS-trap, em-dash, mpirun rules, SIGTERM 10s, ENVIRON ограничения,
ASE issue #1130 и т.п.). Имя короткое, произносимо в обоих языках, домены
`lotsman.dev / lotsman.io` свободны.

Альтернативы (опционально к голосованию):
**Pilot** (англ. эквивалент), **Bosun** (боцман — хозяин палубы),
**Helm** (кормчий), **Skipper**.

---

## Архитектура: Marina + Lotsman split

Два бинаря, один проект:

```
┌──────────────┐                  ┌──────────────────────┐
│ Claude Code  │ ◀──MCP stdio──▶ │ Marina               │
│ Codex /      │ ◀──MCP SSE────▶ │ (локальная daemon)   │
│ кастомный    │                  │                      │
│ orchestrator │                  │ • host registry      │
│              │                  │ • connection pool    │
│ один MCP     │                  │ • jobId routing      │
│ entry в      │                  │ • event aggregation  │
│ mcp.json     │                  │ • Vast.ai control    │
│              │                  │ • secrets / SSH keys │
└──────────────┘                  └──────────┬───────────┘
                                             │ MCP-over-SSH stdio (один pipe per host, persistent)
                            ┌────────────────┼────────────────┐
                            ▼                ▼                ▼
                    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
                    │ Lotsman QE   │ │ Lotsman CP2K │ │ Lotsman ABACUS│
                    │ vast/W3      │ │ vast/W1      │ │ gomer        │
                    │ ephemeral    │ │ ephemeral    │ │              │
                    └──────────────┘ └──────────────┘ └──────────────┘
```

**Marina** — локальная daemon на машине пользователя. Один MCP-entry в `.mcp.json`. Никогда не нужно перезапускать Claude Code при добавлении/удалении хостов.

**Lotsman** — per-container daemon. Запекается в `infra-*-gpu` образы. Standalone MCP server (можно подключиться напрямую для дебага), но обычно общается только с Marina по SSH stdio.

### Lotsman (per-container)

- Бинарь `lotsman` запекается в каждый `infra-*-gpu` образ + systemd/supervisord unit, поднимается на старте контейнера.
- Манифест `/etc/lotsman/manifest.toml` (тул, версия, дефолты OMP/npool/mpirun, default_watchdogs) — статический, версионируется в git.
- При старте: self-check (GPU FP64? mpirun? диск? CUDA cc?) → публикует через `whoami()`.
- Job state: JSON в `/var/lotsman/jobs/<jobId>/` (ephemeral, контейнер one-shot).
- Transport: `mcp-over-stdio` — Marina вызывает `ssh root@host docker exec C lotsman --stdio` и держит pipe persistent.
- Standalone-режим: можно подключить напрямую через `.mcp.json` для дебага одного контейнера.

### Marina (local hub)

- Standalone Python daemon, distributed via pip (`pip install lotsman-marina`).
- Config: `~/.lotsman/marina.toml` (хосты, Vast.ai API key, SSH ключи, webhooks).
- Persistent state: `~/.lotsman/marina.db` (sqlite — host registry, job index, audit log).
- Connection management: SSH connection pool к каждому Lotsman'у, auto-reconnect, heartbeat 30s.
- Recovery: при рестарте Marina переподключается к alive хостам, синхронизирует job state с Lotsman'ами (Lotsman = source of truth для своих jobs).
- Lost host handling: ssh down >5 min → host marked `unreachable`, jobs `lost`; on reconnect — re-sync.

### jobId schema

Format: `<hostId>/<random>` — e.g., `w3/01HPQR3NX9ABC...`

- Marina парсит, роутит к этому host'у Lotsman.
- Self-describing: видно в логах какой host владеет job'ом.
- Globally unique по построению.

### Transport stack

```
┌──────────────┐  MCP stdio/SSE  ┌────────┐  gRPC over SSH+UDS  ┌────────────┐
│ Claude Code  │ ◀──────────────▶│ Marina │ ◀──────────────────▶│ Lotsman    │
│ Codex / etc. │                 │        │                     │ (container)│
└──────────────┘                 └────┬───┘                     └────────────┘
   AI-facing                          │ HTTPS REST
   (MCP)                              ▼
                                ┌──────────┐
                                │ Vast.ai  │
                                │ API      │
                                └──────────┘
```

**Claude Code ↔ Marina = MCP** — AI-facing, multiple agent клиенты (Claude, Codex, custom). MCP-over-stdio primary, MCP-over-SSE для multi-session.

**Marina ↔ Lotsman = gRPC** — service-to-service. Причины почему НЕ MCP:
- Streaming binary first-class (harvest tarball'ов без base64 +33% inflation; events/tail_follow nativ server-stream).
- HTTP/2 multiplexing на high-frequency status polling (10 jobs = один pipe, не 10 handshakes).
- protobuf schema evolution с backwards-compatible field numbers > MCP tool registration.
- Service-to-service trust model (SSH ключ = граница доверия), не AI-permission model.
- `grpcurl` debug, otel observability, interceptors для logging/auth.

**Транспорт gRPC: over SSH-forwarded Unix domain socket.**

```
Lotsman:    gRPC server на /var/run/lotsman.sock в контейнере
Mount:      -v /var/run/lotsman:/var/run/lotsman (host видит socket per container)
Marina:     ssh -L /tmp/lotsman-<host>.sock:/var/run/lotsman/<container>.sock root@host
            gRPC client → unix:///tmp/lotsman-<host>.sock
```

Persistent SSH с auto-reconnect. Heartbeat 30s. Fallback на TCP port forward если UDS forwarding finicky (старый OpenSSH).

**Marina ↔ Vast.ai = HTTPS REST** — через `vastai-python` или прямые requests'ы. API ключ только в Marina config.

### Lotsman без MCP face — single-host debug fallback

В РЕШЕНИЕ-085 Lotsman был standalone MCP server. С gRPC это уходит. Замена:
- **Single-host Marina** с конфигом на 1 host — эффективно standalone debug (Marina лёгкая, Python + sqlite).
- **`lotsman-cli`** — тонкая gRPC CLI обёртка (~100 строк), debug Lotsman'а напрямую через UDS без Marina.
- **Тестирование** — gRPC stack стандартный (grpcio-testing для in-process, grpcurl для wire-level).

Open-source, Apache-2. Repo: github.com/exopoiesis/lotsman. Один monorepo с двумя package'ами + shared proto.

---

## API

API делится на **Marina-only** (sea registry, host fleet ops) и **per-job** (run/status/kill/...). Per-job команды Marina проксирует к Lotsman'у на конкретном хосте — host автоматически инферится из `jobId` или передаётся явно в `run()`.

### Sea abstraction (provider-agnostic hosting)

**Sea** = именованный провайдер хостинга (gomer, loki, vast_main, runpod_a, lambda, …). У каждого Sea есть имя (registered в `marina.toml` под `[seas.NAME]`) и реализация (`DockerSea` для docker-context-based, `VastSea` для Vast.ai, и т.д.). Marina держит реестр зарегистрированных Sea instances и диспатчит host-команды по `sea` параметру.

Команды поэтому называются **`sea_*`** (метаданные провайдера) и **`host_*`** (lifecycle — единый namespace, провайдер-агностик):

#### Sea registry & queries

| Команда | Параметры | Назначение |
|---|---|---|
| `sea_list` | — | имена всех зарегистрированных морей |
| `sea_search` | `sea`, `filters?`, `limit?=20` | offers моря (gomer = один static, vast = много динамически) |
| `sea_recommend` | `sea`, `workload`, `budget_per_hour?`, `min_hours?` | top offers подходящие под `dft_paper_grade\|dft_smoke\|mlip\|aimd_long` |
| `sea_status` | `sea` | reachable? балансы / burn / детали транспорта |
| `cost_summary` | `sea?` | total $/hr + per-host breakdown + balance + burn 24h. Без `sea` — aggregated по всем |

#### Host lifecycle (sea-driven, единый namespace)

| Команда | Параметры | Назначение |
|---|---|---|
| `host_create` | `sea`, `image`, `name?`, `offer_id?`, `disk_gb?`, `onstart?` | provision новый host в указанном море. Возвращает `{name, sea, instance_id, grpc_target, state, cost_per_hour, ...}`. Auto-register в Hub |
| `host_add` | `name`, `target` | manual: зарегистрировать существующий gRPC endpoint (без provisioning) — для pre-baked Lotsman'ов и legacy машин |
| `host_destroy` | `name`, `kill_running?=false` | tear down. Sea-managed → owning sea destroys (`docker rm` / `vastai destroy`); manual → unregister + close channel |
| `host_stop` / `host_start` | `name` | для морей с stop/start (docker, vast). Restart re-resolves grpc port |
| `host_list` | `sea?` | все hosts; с `sea=NAME` — только из этого моря |
| `host_status` | `name` | (M3) detailed: ssh ok? container alive? lotsman responsive? `whoami()` payload |
| `kill_all_on_host` | `name`, `confirm: "yes"` | (M3) nuke всех jobs (для pre-destroy cleanup) |
| `harvest_all_done` | `host?: name` | (M3) auto-harvest всех `done`/`failed` jobs |

API ключи для каждого Sea (Vast.ai token, etc.) хранятся **только** в Marina config (`~/.lotsman/marina.toml`), не передаются через MCP, не светятся в args/transcripts/logs.

#### Workload presets

Built-in пресеты в `marina/seas/presets.py` кодируют project lessons (DEADLY_MISTAKES, PROJECT_STATE):

| preset | hard requirements |
|---|---|
| `dft_paper_grade` | `requires_fp64=True`, vram≥16, GHz≥5.0, cores≥6, RAM≥16, disk≥80, reliability≥0.95 |
| `dft_smoke` | `requires_fp64=True`, vram≥12, GHz≥4.5, cores≥4, RAM≥12, disk≥40, reliability≥0.92 |
| `mlip` | `requires_fp64=False`, vram≥12, GHz≥3.5, cores≥4, RAM≥12, disk≥20 |
| `aimd_long` | `requires_fp64=True`, vram≥16, GHz≥5.0, cores≥6, RAM≥24, disk≥200, reliability≥0.97 |

Owned hardware (DockerSea) defaults `reliability=1.0` (owner-attested) — admin может опустить в config'е если железо flaky.

#### Sea implementations

| sea type | impl | поддерживает |
|---|---|---|
| `docker_sea` | `marina/seas/docker_sea.py` | `docker --context <ctx>` для local или remote Docker hosts (gomer, loki, default). One container = one host. Cost = $0/hr (owned) |
| `vast_sea` | `marina/seas/vast_sea.py` *(M2-B, pending)* | Vast.ai REST API — search/create/destroy/balance |
| `runpod_sea` | *(future)* | RunPod / Lambda / Crusoe — provider plug-ins |

Пример конфига `~/.lotsman/marina.toml`:

```toml
[seas.gomer]
type = "docker_sea"
docker_context = "gomer"
gpu_model = "RTX 4070"
gpu_count = 1
vram_gb = 12
fp64_native = false
cpu_ghz = 5.7
cpu_cores = 8
ram_gb = 32
disk_gb = 500
# reliability = 1.0  # default: owner-attested

[seas.loki]
type = "docker_sea"
docker_context = "default"
gpu_model = "none"
gpu_count = 0
vram_gb = 0
fp64_native = false
cpu_ghz = 4.0
cpu_cores = 8
ram_gb = 16
disk_gb = 200

# (M2-B) [seas.vast]
# type = "vast_sea"
# api_key_env = "VASTAI_API_KEY"
```

**Built-in presets** (кодируют DEADLY_MISTAKES + PROJECT_STATE опыт):

| preset | constraints |
|---|---|
| `dft_paper_grade` | A100/H100/V100 only (нативный FP64), GHz≥5.0, RAM>16, cores≥6, reliability≥0.95, verified, inet_down≥200 Mbps |
| `dft_smoke` | то же но дешевле, H100 PCIe OK, reliability≥0.92 |
| `mlip` | RTX 4090/L40/A40 OK (FP32 fine), VRAM≥12 |
| `aimd_long` | paper-grade + disk≥200 GB + verified strict (multi-week runs) |

`with_recommended_filters=true` (default) дополнительно:
- exclude `loading` инстансов старше 30 мин (s124 lesson)
- min reliability 0.95
- verified hosts only
- exclude L40/A40/RTX-6000-Ada если workload=DFT (нет нативного FP64, 30-60× медленнее)

Override: `with_recommended_filters=false` — raw поиск без наших правил.

#### Vast.ai-specific lifecycle (M2-B, реализуется через `VastSea`)

В M2-A все команды unified через `host_create(sea, ...)` / `host_destroy` / `host_stop` / `host_start`. Vast-специфичные нюансы (renew, harvest_done_first guard) живут внутри `VastSea` impl — **не отдельные MCP команды**:

- `vast_create offer_id image disk_gb` → `host_create(sea="vast", image=image, offer_id=offer_id, disk_gb=disk_gb)`. Под капотом VastSea: `vastai create instance` → wait для `running` (30 min timeout) → SSH ключ install → container ready check → `whoami()` для верификации.
- `vast_destroy host_name` → `host_destroy(name=host_name, kill_running=False)`. VastSea отказывает если live `running` jobs без `kill_running=True`. Refuses если последний user prompt был >30 min назад (handoff ≠ approval, см. CLAUDE.md DEADLY_MISTAKES #7) — это check на Marina-side, реализуется через decorator над host_destroy.
- `host_renew(name, hours)` (M3) — VastSea extends rental, DockerSea raises NotImplementedError.

#### Cost tracking

| Команда | Возврат |
|---|---|
| `cost_summary` | `{sea, total_per_hour, per_host[], burn_rate_24h, balance, days_remaining_at_balance}` |
| `cost_history` | (M3) `{daily_spend[], top_jobs_by_cost[], idle_waste_estimate}` (idle GPU = burn без compute) |

### Per-job (Marina проксирует к Lotsman)

#### Job lifecycle

| Команда | Параметры | Возврат |
|---|---|---|
| `run` | `host: str`, `script: str`, `name?`, `wd?`, `env?: dict`, `singleton_key?`, `resume_from?: jobId`, `watchdogs_extra?`, `watchdogs_disable?` | `jobId` (формат `<hostId>/<random>`) |
| `status` | `jobId` | `{state, started_at, last_activity, pid, exit_code?, resource_usage}` (state ∈ pending/running/done/failed/killed/orphaned) |
| `wait` | `jobId`, `timeout_sec?`, `until_state?` | финальный статус (для долгих — лучше `events`) |
| `kill` | `jobId`, `grace_sec?=10`, `force?=true` | `{killed: bool, leftover_pids: []}` — SIGTERM → wait → SIGKILL; child-tree aware; **исключает `$$` (pgrep self-match)**; verify positive |
| `restart` | `jobId`, `script?`, `env_overrides?`, `preserve_outputs?=true` | новый jobId; replicate env, авто-rename `output.log → output.log.prev` |
| `list_jobs` | `state_filter?` | `[{jobId, name, state, age}]` |

`run` под капотом:
- CRLF→LF
- em-dash `—` → `--`
- нормализация `/tmp/` → `//tmp/` если args идут наружу через MSYS
- инжекция `mpirun --allow-run-as-root --bind-to none -np N` если тул того
  требует (QE GPU silent crash без mpirun)
- singleton lock по `singleton_key`
- child-tree pid registration (для надёжного kill)

### Logs / observability

| Команда | Параметры | Возврат |
|---|---|---|
| `logs` | `jobId`, `tail?`, `head?`, `grep?`, `stream?=false` | string или SSE |
| `tail_follow` | `jobId`, `from_offset?` | SSE новых строк |
| `progress` | `jobId` | tool-специфичный парсинг: QE → SCF iter / E_tot / fmax; CP2K → md step / Cons.Qty drift; NEB → per-image fmax; AIMD → T / Etot |
| `events` | `since?` | SSE stream: state changes, watchdog alerts, OOM, GPU idle, near-SIGTERM warnings |

### Harvest (главный pain-point: **никогда blind**)

| Команда | Параметры | Возврат |
|---|---|---|
| `harvest_inventory` | `jobId`, `mode: essential\|full\|debug` | DONE 2026-06-02. `[{path, size_bytes, included, reason}]` + `included_bytes` preview |
| `harvest` | `jobId`, `mode`, `format: tar\|tar.gz` | DONE 2026-06-02. Создаёт archive в job-dir, возвращает `archive_path`, `archive_bytes`, `sha256`, manifest. MCP base64 content только при explicit `include_content=true` |
| `download` | `host`, `path`, `max_bytes?` | DONE 2026-06-02. bytes/base64 для одиночных файлов, с truncation metadata |
| `download_glob` | `host`, `pattern`, `format: tar\|tar.gz`, `confirm_size_gb?` | DONE 2026-06-02. Создаёт guarded archive; **hard-fail если match >5 GB без явного confirm — защита от scp -r prod_dir на 82 GB wfc** |

Modes:
- **essential** — scripts + .in/.json + .out + последний restart point + monitor
  logs (≈ что мы вручную делаем через `safe_harvest.sh`).
- **full** — все regular files кроме generated harvest archives.
- **debug** — все regular files кроме generated harvest archives; reserved for cores/intermediate wfc use.

### Filesystem (избежать quoting hell)

| Команда | Параметры | Заметка |
|---|---|---|
| `upload` | `host`, `path`, `content\|content_b64`, `create_parents?=true`, `overwrite?=false`, `executable?=false` | DONE 2026-06-02. Основной путь staging для скриптов и input-файлов. gRPC возвращает `bytes_written` + `sha256`; MCP принимает text или base64 |
| `mkdir` | `host`, `path`, `parents?=true`, `exist_ok?=true` | DONE 2026-06-02 |
| `ls` | `host`, `path` | DONE 2026-06-02. Возвращает `name/path/is_dir/size_bytes/mtime_unix_ms`; `glob` и recursive — позже |
| `stat` | `host`, `path` | DONE 2026-06-02. Возвращает exists/is_dir/size/mtime; `sha256` — позже |
| `cat` | `host`, `path`, `max_bytes?` | DONE 2026-06-02. Для коротких текстов; MCP возвращает и UTF-8 text, и base64 |
| `disk_free` | `host`, `path?` | DONE 2026-06-02. Возвращает total/used/free bytes; top-N largest dirs — позже |
| `rm` | `path`, `safe?=true` | DEFERRED. Только через safe-rm policy (`safe_rm` / .trash / fuser gate); unsafe требует отдельного решения |

### System / self-knowledge

| Команда | Параметры | Возврат |
|---|---|---|
| `whoami` | — | `{tool, tool_version, image, image_tag, gpu_model, gpu_fp64, mpirun_required, default_omp, default_npool, env_constraints[], known_pitfalls[], default_watchdogs[]}` |
| `health` | — | `{disk_ok, gpu_ok, scf_test_ok, mpirun_ok, env_ok}` |
| `bench_quick` | — | 30–60 sec smoke (1-step SCF на reference structure) — sanity перед heavy run |
| `gpu_status` | — | parsed nvidia-smi + idle_seconds_window |
| `processes` | `n?=20` | top CPU/RSS |
| `help` | `topic?: str` | markdown-summary API + recommended workflows |
| `examples` | `workflow?: str` | готовые usage snippets (run+monitor, run+harvest, kill+restart, neb-with-anchor-gate, etc.) |

**Discovery (важно).** MCP-протокол уже даёт machine-readable enumeration через `tools/list` — клиент (Claude Code, Codex) при handshake автоматически получает все команды с JSON-схемами параметров. **Никаких custom commands для discovery не нужно** — это работа протокола.

`help(topic?)` дополняет (не заменяет) `tools/list`:
- `help()` — high-level overview API + рекомендованные workflows.
- `help("watchdogs")` — когда какие watchdogs включаются по умолчанию для текущего тула, как добавить extras, как читать `watchdog_history`.
- `help("harvest")` — когда `essential` vs `full` vs `debug`, защита от blast-radius, как preview через `harvest_inventory`.
- `help("events")` — какие 4 tier'а доступны на этом инстансе, какой active в текущем коннекте.
- `help("qe")` / `help("cp2k")` — tool-specific gotchas (mpirun mandatory, ENVIRON slab-only, EXT_RESTART rules, и т.п.).

`examples(workflow?)` — copy-paste готовые snippets для типовых сценариев. Снижает barrier-to-entry для новых пользователей и для LLM на скромном капасити.

### Watchdogs (автономия — defaults включены, не нужно вспоминать)

**Принцип:** каждый tool manifest определяет `default_watchdogs[]` которые **активируются автоматически** при `run()` / `restart()`. Дополнительные присваиваются explicit; ненужные явно отключаются. Защита от "запустил и забыл watchdog".

`run()` / `restart()` принимают:
- `watchdogs?: [...]` — ПОЛНАЯ замена дефолтов (rare, escape hatch)
- `watchdogs_extra?: [...]` — добавить к дефолтам
- `watchdogs_disable?: [name1, name2]` — явный opt-out конкретных дефолтов

| Команда | Параметры | Назначение |
|---|---|---|
| `watchdog_list` | `jobId?` | какие сейчас активны (включая defaults) |
| `watchdog_history` | `jobId` | что сработало и когда (с timestamps + payload) |
| `watchdog_add` | `jobId`, `type`, `threshold`, `action: notify\|kill\|checkpoint` | docked-on во время выполнения (rare) |
| `watchdog_remove` | `watchdogId` | turn off (rare) |

**QE manifest defaults:**
- `gpu_idle` > 30 min → `notify` (А100 idle = $0.70/hr drain)
- `scf_plateau` > 200 iter no E_total improvement → `notify` (Q100 lesson)
- `disk_low` < 5 GB → `notify` (s126 disk-full crash precedent)
- `process_oom` → `kill` + `notify`
- `mpirun_missing` (catches QE GPU silent crash) → fail-fast at `run()`, never reaches watchdog

**CP2K manifest defaults:**
- `gpu_idle` > 30 min → `notify`
- `cons_qty_drift` > 5 meV/ps (after 1ps warmup) → `notify` (W1/W2 lesson)
- `disk_low` < 5 GB → `notify`
- `process_oom` → `kill` + `notify`
- `output_log_race` (rename collision на restart) → `notify` (s130 lesson)

**Job-specific extras (через `watchdogs_extra`):**
- `h_anchor_violation` (NEB) min(d_H_S) > 1.7 Å → `notify` (s130 mack canonical artifact gate; включается явно для NEB jobs)
- `neb_endpoint_same_basin` dE_endpoints < SCF noise + same nearest_Fe → `notify` (V_S+H trap detector)
- `temperature_drift` (AIMD) > 50 K от target → `notify`

**Watchdog event delivery:** см. секцию **Event delivery** ниже — 4 tier'а от polling до real-time push через `claude/channel`.

### Event delivery (push to orchestrator)

Watchdog hits, state changes, critical errors доносятся до MCP-клиента через **4 tier'а**, escalating в real-timeness. Lotsman поддерживает все четыре; клиент выбирает что использовать через capability negotiation.

**Tier 1 — Polling (всегда доступен, baseline)**
- `events(since?)` — SSE stream новых событий (когда клиент подписался).
- `status(jobId)` включает `recent_events[]` (последние N).
- Идеально под наш текущий `/loop` + `ScheduleWakeup` pattern: Claude просыпается каждые 30 мин, дёргает `events()`, реагирует.
- Никакой настройки клиента. Работает на stdio транспорте.
- **Это default для M1.**

**Tier 2 — MCP Tasks API (spec 2025-11-25)**
- `run()` регистрирует job как MCP Task (`taskId == jobId`).
- Watchdog hits обновляют task state.
- Клиент native-polls `tasks/result` / `tasks/status` (стандарт MCP, не наш custom).
- Работает с любым MCP-aware клиентом — Claude Code, Codex, custom orchestrators.
- **M2 milestone.**

**Tier 3 — Push via `claude/channel` (Claude Code 2.1.110+, март 2026)**
- Lotsman декларирует `claude/channel` capability в handshake.
- Клиент стартует с `--channels lotsman` (или эквивалент в `.mcp.json`).
- Watchdog с `action=alert_now` push'ит real-time → **Claude Code просыпается out-of-band**, без ScheduleWakeup.
- Use cases: `gpu_idle` (drain $0.70/hr), `process_oom`, `unrecoverable_scf_fail`.
- **Требует** активной Claude Code сессии или настроенного Remote Control.
- **Caveat:** capability относительно свежая — в M2 верифицировать что текущее поведение реально пробуждает агента (есть риск что `--channels` поведение изменилось между релизами).
- **M2 prototype, M3 stable.**

**Tier 4 — External webhooks (escape hatch)**
- В manifest можно прописать `webhooks` per watchdog type — Slack/Telegram/email/PagerDuty.
- Срабатывает независимо от того, активна ли MCP сессия.
- Use cases: hard failures когда Claude Code не запущен; multi-человек team alerting.
- Не требует MCP клиента вообще.
- **M3 milestone.**

**Recommended stack для DFT overnight runs:**
- **Tier 1** polling baseline (всегда).
- **Tier 3** `claude/channel` для critical alerts (когда Claude активен).
- **Tier 4** webhook на email/telegram для hard failures когда никого нет на месте.
- **Tier 2** добавляется автоматически когда клиент его поддерживает (нативный MCP standard).

**Acceptance criteria для M2:**
- `gpu_idle > 30 min` watchdog в 03:00 ночи реально пробуждает Claude Code, который без user prompt: видит alert → выясняет что job zombi → kill+restart или пишет user в утро.
- Verified end-to-end на одном живом Vast.ai инстансе.

### Tool-specific (Лоцман знает свой тул)

| Команда | Параметры | Назначение |
|---|---|---|
| `prepare_input` | `kind: qe_pw\|qe_neb\|abacus\|cp2k\|gpaw\|jdftx\|...`, `params`, `coords: ase.Atoms` | валидированный input |
| `validate_input` | `content`, `kind` | warnings/errors |
| `pseudopotentials` | `element?`, `family?` | path + provenance |
| `lessons_for` | `kind` | релевантные pitfalls для образа |

`prepare_input` ловит то, на чём мы уже теряли часы:
- ABACUS: `uramping=single double` (не list), `hubbard_u/orbital_corr` size=ntype hard crash, abacuslite STRU alphabetical.
- QE: `nspin=2 metal limitation`, `disk_io='low'` breaks SIGTERM recovery, `mixing_mode` kwarg ignored, smearing required для odd-electron defects.
- ENVIRON: bulk 3D rejected (slab/molecule only).
- CP2K: inline comment crashes, EXT_RESTART rules, OUTER_SCF в metadyn.
- ASE Espresso: shallow `dict()` merge bug.

### Resilience (Vast.ai 10s SIGTERM)

| Команда | Параметры |
|---|---|
| `checkpoint_force` | `jobId` |
| `sigterm_drill` | `jobId` — dry-run проверка append + --resume scenario |

---

## Что Лоцман автоматически чинит

| Lesson | Где сейчас руками | Лоцман делает |
|---|---|---|
| MSYS `/tmp/` translation | hook `block-msys-trap.sh` | прозрачно в transport layer |
| em-dash → `--` | ручной sweep | в `run`/`upload` |
| QE GPU без mpirun = silent crash | DFT_DEPLOY_CHECKLIST | авто-инжекция |
| `scp -r prod_dir` 82 GB ловушка | hook `block-dangerous-harvest.sh` | `download_glob` size guard |
| pgrep self-match `$$` | F-047 | child-tree kill primitive |
| `output.log` race на restart | F-047 | rename to `.prev` |
| Singleton race | manual lockfile | instance-level lock primitive |
| `safe_rm` vs `rm -rf` | safe_rm.sh | дефолт в `rm` |
| docker commit ломает GPU | warn в CLAUDE.md | hard-refuse на server-side |
| ASCII-only контейнер | manual em-dash sweep | upload-time guard |

Сейчас всё это ловится хуками + памятью + ручной дисциплиной. Лоцман делает их
impossible-to-skip.

---

## Resolved decisions (2026-05-05)

1. ✅ **Имя:** **Lotsman**.
2. ✅ **Transport:** stdio-via-ssh primary. HTTP/SSE — потом, не в M1.
3. ✅ **Manifest source:** статический `manifest.toml`, запекается в Dockerfile, версионируется в git.
4. ✅ **Repo location:** отдельный GitHub репо (clone в `project-third-matter/git/lotsman/`).
5. ✅ **MVP tools:** **QE + CP2K параллельно** — валидирует cross-tool API consistency с первого дня (не появятся QE-специфичные допущения, которые пришлось бы переписывать на M3).
6. ✅ **Job retention:** state живёт пока живёт контейнер. Контейнер ephemeral → state ephemeral. Никаких TTL, никаких manual `forget()`. Job ID гарантированно уникален в рамках инстанса; если новый инстанс — новый Лоцман с пустым состоянием.

---

## Roadmap (черновик)

- **M0** — design doc + repo scaffold (DONE 2026-05-05).
- **M1** — **Lotsman + Marina baseline DONE 2026-05-05.** 6 RPCs (Run, Status, Kill, Logs, TailFollow, Whoami), 66 tests passing in <6s, two daemon CLI entry points, Dockerfile validated end-to-end on remote Linux Docker (gomer). KISS scope: single-job-per-Lotsman, TCP gRPC (UDS+SSH deferred), in-memory state. Three M1-marked but deferred items: per-tool image layering (`infra-qe-gpu` + Lotsman), SSH-tunneled UDS transport, harvest streaming RPC.
- **M2-A — sea abstraction + DockerSea DONE 2026-05-05.** Provider-agnostic `Sea` Protocol (search/recommend/create/destroy/stop/start/cost_summary/status), `DockerSea` impl over `docker --context <ctx>` (gomer/loki/local). Workload presets `dft_paper_grade / dft_smoke / mlip / aimd_long` encoding project DEADLY_MISTAKES (FP64 only, GHz≥5.0, reliability≥0.95). Marina config `[seas.NAME]` sections + factory dispatch. MCP API: `sea_list / sea_search / sea_recommend / sea_status / cost_summary` + `host_create / host_add / host_destroy / host_stop / host_start / host_list`. Bumps total tests 66→146, all green in 6s. Provisioning testable on free local docker — no Vast.ai burn.
- **M2-B — watchdog system + events fan-out DONE 2026-05-05.** Per-Lotsman `Supervisor` with three production checks (`DiskLowCheck`, `ProcessExitOomCheck`, `GpuIdleCheck`); fires at most once per check, idempotent. New gRPC RPCs `Events` (server-stream), `WatchdogList`, `WatchdogHistory`, `EventsHistoryAll`. Marina-side `Hub.events_all` fan-out across hosts. MCP tools `watchdog_list / watchdog_history / events / events_all`. Sea Protocol gains `env: dict[str, str]` on `create()` so admin can tune watchdog thresholds per-host (`LOTSMAN_DISK_LOW_GB`, `LOTSMAN_GPU_IDLE_*`). L3 smoke: `disk_low` forced fire on a real Docker container observed via Marina end-to-end. 195 tests total, ~84 s wall. **Tier 1 polling** is the delivered transport — sufficient for the `/loop`+`ScheduleWakeup` pattern Claude Code uses today.
- **M2-Files — filesystem staging API DONE 2026-06-02.** First command layer for uploading scripts/inputs to host disk without shell quoting: gRPC `Upload / Mkdir / Ls / Stat / Cat / DiskFree`, Marina Hub proxies, MCP tools `upload / mkdir / ls / stat / cat / disk_free`. `upload` supports UTF-8 text or base64 payloads, parent creation, overwrite guard, executable bit, and sha256 response. `rm` intentionally deferred until safe-rm policy is implemented. Windows local dev hardening: `resolve_bash()` now prefers Git Bash over the WSL shim. Typed protobuf `.pyi` facades added so `mypy src tests` is green. 204 tests total; ruff, mypy, pytest clean.
- **M2-Harvest — guarded harvest/download API DONE 2026-06-02.** gRPC `HarvestInventory / Harvest / Download / DownloadGlob`, Marina Hub proxies, MCP tools `harvest_inventory / harvest / download / download_glob`. `harvest` creates tar/tar.gz archives in job-dir and returns manifest + checksum; MCP content transfer is opt-in via `include_content=true`. `download_glob` has 5 GB hard guard unless `confirm_size_gb` covers matched total. 210 tests total; ruff, mypy, pytest clean.
- **M2-B-Tier2 (deferred — upstream blocker).** Native MCP Tasks API: `mcp` Python SDK 1.26.0 ships Task *types* but neither `FastMCP` nor lowlevel `Server` surfaces task **handlers**. Revisit when SDK exposes them.
- **M2-C — `claude/channel` push prototype (deferred — upstream blocker).** Undocumented in standard MCP; Claude Code-specific capability mentioned in this design doc with explicit caveat. Standard `LoggingMessageNotification` flows only within active sessions, doesn't wake a sleeping agent. In the meantime, Tier 1 polling already meets the "save my night" goal — real-time push would just reduce wake latency from ~30 min to seconds.
- **M2-A-Vast — `VastSea` (deferred per project decision).** "Сначала всё через гомер" — until owned-hardware flow is fully proven, no Vast.ai burn.
- **M3** — `prepare_input` / `validate_input` / `lessons_for` для QE и CP2K + **external webhooks** + `cost_history` + `host_status / kill_all_on_host / harvest_all_done`. Marina становится самодостаточной для всего daily compute workflow (search → create → run → monitor → harvest → destroy).
- **M4** — третий tool (ABACUS или GPAW) + опциональный SSH-multiplex для Marina↔Lotsman (snappier) + RunPod/Lambda/Crusoe seas.
- **M5** — HTTP/SSE transport между Marina↔Lotsman (опция помимо ssh stdio).
- **M6** — public release, blog post, первый external user.

---

## Repo layout

```
lotsman/
├── README.md
├── LICENSE                      Apache-2.0
├── pyproject.toml               uv workspace (root)
├── proto/
│   └── lotsman/v1/
│       └── lotsman.proto        gRPC service contract — source of truth
├── lotsman/                     in-container daemon (gRPC server)
│   ├── pyproject.toml
│   ├── src/lotsman/
│   │   ├── server.py            gRPC service impl
│   │   ├── manifest.py          /etc/lotsman/manifest.toml parser
│   │   ├── jobs/                lifecycle, state, child-tree pid mgmt
│   │   ├── watchdogs/           supervisor + presets per type
│   │   ├── harvest/             essential/full/debug modes + size guard
│   │   ├── tools/               adapters: qe.py, cp2k.py
│   │   └── platform/            em-dash / msys / mpirun / CRLF / safe_rm
│   ├── tests/                   { unit, service, integration }
│   └── Dockerfile.test          throwaway container для integration tests
├── marina/                      local hub (MCP server + gRPC client)
│   ├── pyproject.toml
│   ├── src/marina/
│   │   ├── mcp_server.py        MCP face к Claude Code (sea_*/host_*/run/...)
│   │   ├── hub.py               sea registry + host registry + per-job routing
│   │   ├── config.py            marina.toml parser ([hosts.*] + [seas.*])
│   │   ├── router.py            jobId → host
│   │   └── seas/                provider abstraction
│   │       ├── base.py          Sea Protocol + Offer/HostHandle/CostBreakdown
│   │       ├── presets.py       workload presets (dft_paper_grade/...)
│   │       ├── registry.py      module-level Sea registry (test helper)
│   │       ├── factory.py       build_sea(name, type, raw) → Sea
│   │       ├── runner.py        injectable subprocess runner (testability)
│   │       ├── docker_sea.py    DockerSea: docker --context <ctx>
│   │       └── vast_sea.py      (M2-B) VastSea: vastai-python
│   └── tests/                   { unit, service, integration }
├── lotsman-cli/                 thin gRPC CLI для standalone Lotsman debug
│   └── src/lotsman_cli/
├── docs/
│   ├── DESIGN.md                этот файл
│   ├── SECURITY.md
│   └── TESTING.md               TDD discipline + test layer details
└── .github/workflows/
    ├── unit.yml                 L1 на PR
    ├── service.yml              L2 на PR
    ├── integration.yml          L3 на push to main
    └── release.yml              tagged release builds
```

---

## Development methodology — TDD

**Принцип:** каждая команда (gRPC RPC) рождается через red-green-refactor. Proto-определение + failing test + implementation, в этом порядке. Без исключений.

### 4 уровня тестов

| Layer | Что тестируем | Скорость | Когда запускаем |
|---|---|---|---|
| **L1 unit** | pure functions: em-dash strip, MSYS path normalize, jobId encode/decode, manifest parse, watchdog state machine, harvest selector | ms | каждое save / pre-commit |
| **L2 service** | gRPC service в-process через `grpcio-testing`: real gRPC client против real server impl, с fake tool subprocess (`echo`/`sleep`/scripted output) | <3s suite | pre-commit + PR |
| **L3 integration** | real Lotsman в throwaway Docker container (testcontainers-python), real gRPC через UDS, real ssh stdio к Marine | 30s-2min | push to main |
| **L4 e2e** | real Marina ← MCP ← test client (mock Claude Code), real ssh к real Vast.ai инстансу или ephemeral local container | minutes | release PR + nightly |

### Red-green-refactor для новой команды

1. **Red:** добавить RPC в `proto/lotsman/v1/lotsman.proto`, regen stubs (`buf generate`), написать failing test в `tests/service/`. Test проверяет contract: правильный возврат, правильные ошибки, idempotency где применимо.
2. **Green:** минимальная implementation в `lotsman/src/lotsman/server.py` чтобы тест прошёл. Никакой "future-proofing" логики — только то что покрыто тестом.
3. **Refactor:** очистить, выделить helpers, добавить unit-тесты L1 для extracted helpers.

### Concrete: первый тест M1 (`Run`)

```python
# lotsman/tests/service/test_run_basic.py
import lotsman.v1.lotsman_pb2 as pb

def test_run_returns_jobid_with_host_prefix(lotsman_grpc_stub, fake_qe_binary):
    resp = lotsman_grpc_stub.Run(pb.RunRequest(
        script="#!/bin/bash\necho hello\n",
        name="smoke",
    ))
    assert resp.job_id.startswith("local/")
    assert len(resp.job_id.split("/")[1]) == 26  # ULID
    assert resp.state == pb.JobState.PENDING

def test_run_strips_em_dash(lotsman_grpc_stub, fake_qe_binary, tmp_jobs_dir):
    resp = lotsman_grpc_stub.Run(pb.RunRequest(
        script="echo —flag value",  # em-dash trap
        name="emdash",
    ))
    saved_script = (tmp_jobs_dir / resp.job_id.split("/")[1] / "script.sh").read_text()
    assert "—" not in saved_script
    assert "--flag" in saved_script  # converted

def test_run_singleton_blocks_duplicate(lotsman_grpc_stub, fake_qe_binary):
    lotsman_grpc_stub.Run(pb.RunRequest(script="sleep 10", singleton_key="exclusive"))
    with pytest.raises(grpc.RpcError) as exc:
        lotsman_grpc_stub.Run(pb.RunRequest(script="sleep 10", singleton_key="exclusive"))
    assert exc.value.code() == grpc.StatusCode.ALREADY_EXISTS
```

### Coverage targets

- **L1:** ≥95% (pure logic, no excuses).
- **L2:** ≥85% gRPC service paths.
- **L3:** critical workflows only (run+harvest end-to-end, kill+restart, watchdog fires correctly, ssh stdio reconnect). Не гонимся за coverage цифрой.
- **L4:** smoke per release.

### Tooling

- `pytest` + markers (`-m unit`, `-m service`, `-m integration`, `-m e2e`)
- `grpcio-testing` для in-process L2
- `testcontainers-python` для L3
- `buf` для proto schema management + breaking change detection
- `ruff` lint + format, `mypy --strict` (хорошо покрыт благодаря protobuf-generated types)
- `pre-commit` hooks: ruff format + ruff check + mypy + L1 + L2

### Что НЕ делаем

- **Не пишем код без failing test первым.** Если фикс багу — сначала regression test.
- **Не мокаем то что можно поднять реально.** L2 крутит fake `qe.x` (bash script печатающий правильный stdout) — лучше real subprocess чем mock.
- **Не игнорируем flaky tests.** Flaky = real bug, или фикcим, или удаляем тест.
- **Не пишем тест после кода "чтобы было".** Это не TDD, это theatre.

### Concrete первая задача M1 day 1

1. Setup repo skeleton (`pyproject.toml` workspaces, `proto/lotsman/v1/lotsman.proto` с одним `Run` RPC, generated stubs vendored).
2. Failing test: `test_run_returns_jobid_with_host_prefix` (выше).
3. Implementation: `lotsman/server.py` с минимальным `Run` handler.
4. Test passes → commit.
5. Next test: `test_run_strips_em_dash` — repeat.

Каждый day end = passing test suite + 1-3 новых проходящих RPC. Видимый прогресс на каждом шаге.

---

## Use case: канонический mack V_Fe NEB end-to-end через Marina

**Текущий процесс (s133, без Marina):** ~70 ручных шагов distributed across CLAUDE.md / hooks / memory:
1. Manual `vastai search offers` с filter'ами (часто ошибаюсь в GHz/FP64).
2. `vastai create instance` через CLI, ssh ключ install.
3. Glob existing scripts, выбрать паттерн, edit em-dash + MSYS + mpirun.
4. Write tmp/*.sh, scp на host через `//tmp/`.
5. nohup + singleton lockfile + cpu monitor.
6. ScheduleWakeup на /loop checkout каждые 30 мин.
7. Каждый wakeup: ssh, tail, parse FIRE iter / fmax вручную.
8. Convergence → selective scp .json + .png + neb.log + image_04 + neb.traj.
9. Pre-destroy: harvest, verify, потом `vastai destroy` (с явным "yes").
10. Update memory + handoff manually.

**Через Marina (целевое M3 состояние):**

```python
# 1. Поиск + создание инстанса (Marina хранит API key, мне не светят)
offer = marina.vast_recommend(workload="dft_paper_grade", budget_per_hour=0.80)[0]
host = marina.vast_create(
    offer_id=offer.id,
    image="exopoiesis/infra-qe-gpu:server",
    disk_gb=200,
    host_name="w3"
)  # → ssh ready, lotsman handshake done, watchdogs auto-attached

# 2. Запуск job — host из jobId, watchdogs дефолтные + extras для NEB
jobId = marina.run(
    host="w3",
    script=Path("neb_canonical_mack_72at_qe_VFe.py").read_text(),
    name="mack_VFe_neb",
    singleton_key="mack_neb",
    watchdogs_extra=[
        ("h_anchor_violation", {"threshold_A": 1.7}, "notify"),
        ("neb_endpoint_same_basin", {}, "notify"),
    ]
)
# default watchdogs (gpu_idle/scf_plateau/disk_low/oom) — не нужно вспоминать

# 3. /loop wakeup (или channel push в 03:00 если что-то сломалось):
status = marina.status(jobId)  # → {state: "running", progress: {iter: 12, fmax: 0.176, dE: -0.0019}}
events = marina.events_all(since=last_check)  # все hosts разом
# Если watchdog gpu_idle сработал ночью — Claude уже разбудился через claude/channel push

# 4. После convergence:
if status.state == "done":
    inv = marina.harvest_inventory(jobId, mode="essential")  # preview ~50 MB
    archive = marina.harvest(jobId, mode="essential", format="tar.gz")

# 5. Pre-destroy cleanup
marina.harvest_all_done(host="w3")
marina.vast_destroy(host_name="w3", confirm="yes")  # explicit guard
```

**Что pitfalls делегированы Marine + Lotsman'у (вне моей головы):**
- em-dash, MSYS path, mpirun injection, CRLF, singleton, output.log race, child-tree kill — Lotsman.
- Vast.ai filter mistakes (FP64 GPU, GHz, reliability) — Marina presets.
- Destroy без approval, scp -r prod_dir, host без harvest — Marina API guards.
- Forgotten watchdog → night idle drain — default_watchdogs в manifest + claude/channel push.
- API key leak — Marina одна знает, не уходит наружу.
