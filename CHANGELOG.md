# Changelog

All notable changes to Lotsman are documented here. Format is loosely based on
[Keep a Changelog](https://keepachangelog.com/) and the project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased] — VastSea search, host-fitness scoring & lifecycle (2026-06-17)

Turns the Vast.ai backend into a usable daily tool: rich filtered search,
two synthetic host-fitness scores for DFT, a ready-to-display result table, and
a verified create→list→destroy lifecycle. Full design in
[`docs/HOST_SCORING.md`](docs/HOST_SCORING.md).

### Added — secrets / config

- **`.env` loading** (`marina/dotenv.py`): `marina serve` loads a gitignored
  `.env` (next to the config, e.g. `~/.lotsman/.env`, then cwd) before reading
  any sea config, so `VAST_API_KEY` need not be exported into the MCP launcher's
  environment. Self-contained parser (no `python-dotenv` dep); an exported var
  wins over the file; only key *names* are logged, never values.
- **`vastai_bin`** config: pin the full path to the `vastai` CLI when the marina
  process PATH omits the per-user Python Scripts dir.

### Added — search & filters (MCP `sea_search`)

- **Family-aware `gpu_name`**: `"A100"` expands to
  `gpu_name in [A100_PCIE,A100_SXM4]` (Vast has no bare "A100") via a curated
  `_GPU_FAMILIES` map (A100/A800/H100/H200/V100/P100/B200); a specific model
  matches exactly; a substring guard drops uncatalogued variants.
- **Family-aware `cpu_name`** (python-side substring): `"trpro"` → AMD
  Threadripper PRO 7/5/3 WX; else a literal substring ("5955WX", "EPYC 7763").
- **`vram_gb`** exact filter, **`min_cuda`** (filters `cuda_max_good`, the
  highest CUDA toolkit the host runs well — match to your image), and **`order`**
  sort (`-` prefix = descending) over `cpu_ghz`/`dph`/`vram_gb`/`cpu_cores`/
  `dlperf`/`dlperf_per_dollar`/`zcpu`/`zgpu`/`gpu_mem_bw`/`pcie_bw`/`cuda`.
- Python-side filters/sorts widen the fetch to the whole verified market
  (cap 2000) so a perf sort isn't truncated to the cheapest N — premium GPUs
  (A100/H100/B200) now surface in `-zgpu`.

### Added — host-fitness scores (`marina/seas/perf_score.py`)

- **`zGPU`** — GPU-DFT (QE) score: geometric blend of per-GPU FP64 throughput
  (datasheet table; consumer ≈ FP32/64) and VRAM bandwidth, sublinear multi-GPU
  scaling, PCIe throttle. ~100 = one A100 PCIe; consumer cards score low.
- **`zCPU`** — CPU-DFT (CP2K) score: roofline with a bandwidth-per-core cap and
  a saturation knee at 8 cores (CP2K stops scaling past ~8; >12 adds ~1-2%), so
  core count past the knee does not inflate it; scaled by core homogeneity
  (Threadripper PRO / EPYC / Xeon = 1.0). ~100 = a well-fed knee at ~7 GHz.

### Added — output & recommend

- **Ready-to-display table** rendered server-side (saves agent tokens), fixed
  columns: `ID GPU VRAM CUDA CPU cores RAM Disk zGPU zCPU DLP/$ vbw PCIe $/hr
  geo` (`cores` = ours/total). `format="json"` for raw dicts.
- New `Offer` fields surfaced: `cpu_name`, `cpu_cores_total`, `geolocation`,
  `dlperf`, `dlperf_per_dollar`, `gpu_mem_bw_gbs`, `pcie_bw_gbs`,
  `cuda_max_good`, `zcpu`, `zgpu`. (`reliability` parsed + filterable but no
  longer shown — never used as a column.)
- **`sea_recommend`** now ranks the offers that pass a preset's hard gate by
  host fitness (`zgpu` for GPU-FP64 DFT, `dlperf` for FP32 MLIP) instead of raw
  price, via a per-preset `rank_by` (overridable: `zgpu`/`zcpu`/`dlperf`/
  `dlperf_per_dollar`/`price`).

### Fixed / hardened — lifecycle robustness

- **No indefinite MCP hangs on `vastai`**: each call runs under `cmd_timeout_s`
  (default 45 s) and collects output via temp files, not pipes — a console-shim
  child (`vastai.exe` → python) holding a pipe open can no longer wedge the MCP
  server. Clear errors on timeout / missing binary.
- **`create()` fails fast** on a doomed startup: a bad image, registry-auth, or
  out-of-disk leaves `actual_status` at "loading" forever while the daemon
  retries; we now detect the `status_msg` error and raise at once instead of
  polling to the 30-min ready timeout.
- **Short instance label** = the Marina host name (was `lotsman/<sea>/<name>`).
- Verified end-to-end against real Vast.ai: `host_create` → `host_list` →
  `host_destroy` (instance actually torn down, tunnel closed, billing stops).

### Tests

- 285 passing (ruff + mypy strict + pytest). New: `.env`, GPU/CPU family
  expansion, perf_score (zGPU/zCPU references, knee, consumer gap), table
  render + columns, recommend ranking, `min_cuda` query, fail-fast create,
  short-label round-trip, temp-file/timeout runner.

## [Unreleased] — VastSea: Vast.ai search & create (2026-06-16)

The marketplace backend for the `Sea` abstraction. Marina can now search
Vast.ai offers, recommend by workload preset, rent an instance and wait for it
to come up, list/destroy/stop/start, and report balance + burn rate — all
through the same injectable `Runner` used by `DockerSea`, so the suite never
touches the network or a real account.

### Added

- **`marina/seas/vast_sea.py` — `VastSea`** implementing the full `Sea`
  Protocol over the `vastai` CLI (`--raw` JSON):
  - `search(filters, limit)` — builds a Vast query (`rentable`/`verified`/
    `reliability>`/`dph_total<`/`num_gpus`/`gpu_name`, plus a raw `query`
    escape hatch), `--order dph_total --limit N` (matches `infra/vast_find.sh`);
    parses offers with proportional per-instance RAM/cores and a native-FP64
    GPU table (A100/H100/V100/… only).
  - `recommend(workload, budget, …)` — pushes cheap constraints into the query,
    verifies the rest locally via the shared `PRESETS`/`matches()`, sorts by price.
  - `create(image, offer_id, …)` — `vastai create instance … --ssh`, encodes the
    Marina host name into the instance label, polls `show instances` until
    `running` (30-min default timeout), then waits for sshd and opens an
    `ssh -N -L <local>:127.0.0.1:<container_grpc_port>` tunnel so the gRPC
    target becomes `127.0.0.1:<local>` (injectable clock/sleeper/forwarder).
  - `list_instances` (reconciles name↔id from labels, preserves live tunnel
    port), `destroy`/`stop` (tear the tunnel down), `start` (re-waits + reopens
    the tunnel), `status`, `balance`, `cost_summary` (days-remaining-at-balance),
    `close_all_forwards`; `renew` raises `NotImplementedError` (no lease).
- **`marina/seas/forwarding.py`** — injectable `Forwarder`/`Forward` SSH
  local-forward abstraction (`SubprocessForwarder` spawns a detached
  `ssh -N -L` child with `accept-new`/`ExitOnForwardFailure`/keepalive), plus
  `pick_free_port()`. Mirrors the `Runner` pattern so tests never spawn ssh.
  - API key resolved from an env var (`api_key_env`, default `VAST_API_KEY`),
    never in TOML plaintext nor across the MCP boundary.
  - **Self-contained SSH identity** (replaces the old skypilot relay that held
    keys for all providers): a local `ssh_key_path`/`ssh_pubkey_path` is owned
    by Marina; `create()` runs `vastai attach ssh <id> <pubkey>` so the rented
    box trusts Marina's key directly. Skipped when no local key is configured
    (instances then inherit account-attached keys). `pubkey_loader` injectable
    for tests.
- **Factory**: `build_sea(..., "vast_sea", ...)` constructs a `VastSea`
  (`api_key_env`, `ssh_user`/`ssh_key_path`/`container_grpc_port`,
  `ready_timeout_s`/`ssh_ready_timeout_s`/`poll_interval_s`).
- **Tests** (50): service-level subprocess dispatch via `FakeRunner` +
  `FakeForwarder` (search/recommend/create/poll/ssh-retry/tunnel/list/destroy/
  stop+start/status/cost) + pure-logic unit tests (FP64 table, offer parsing,
  query building, label round-trip, forwarding argv, factory wiring).

## [Unreleased] — filesystem staging API (2026-06-02)

First vertical slice of the file-command layer: staging scripts and input
files onto a compute host through Marina, then verifying them without quoting
through SSH/docker shell layers.

### Added

- **gRPC filesystem RPCs**: `Upload`, `Mkdir`, `Ls`, `Stat`, `Cat`,
  `DiskFree`.
- **Marina Hub proxy methods** for the same operations.
- **MCP tools**: `upload`, `mkdir`, `ls`, `stat`, `cat`, `disk_free`.
  `upload` accepts either UTF-8 `content` or `content_b64` and can create
  parents, refuse overwrite by default, and mark scripts executable.
- **Tests**:
  - service-level upload/cat/stat/ls roundtrip, overwrite guard,
    executable flag, mkdir, disk_free, and missing-file NOT_FOUND;
  - Marina Hub filesystem proxy tests;
  - MCP tool registry expanded for filesystem commands.

### Fixed

- Windows `resolve_bash()` now prefers Git Bash and rejects the WSL shim
  (`C:\Windows\System32\bash.exe`) when it would fail to find `/bin/bash`.
  This restores local Windows service/integration tests for `run()` jobs.
- `mypy src tests` is now green:
  - added typed `.pyi` facades for generated `lotsman_pb2` /
    `lotsman_pb2_grpc` runtime modules;
  - kept strict checking for `src` while relaxing pytest fixture/test
    annotation requirements;
  - made gRPC abort control-flow explicit through a `NoReturn` helper;
  - added missing concrete type arguments for `subprocess.Popen`.

### Tested

- `ruff check .`
- `mypy src tests`
- `pytest -q` → 204 passed

## [Unreleased] — harvest and guarded download API (2026-06-02)

The first working harvest layer for completed jobs: preview what would be
collected, build a job archive, and retrieve files/archives without blind
recursive copy.

### Added

- **gRPC harvest RPCs**:
  - `HarvestInventory(job_id, mode)` previews included/excluded files.
  - `Harvest(job_id, mode, format)` creates a `tar`/`tar.gz` archive in the
    job directory and returns path, size, sha256, and manifest entries.
  - `Download(path, max_bytes?)` reads one file snapshot.
  - `DownloadGlob(pattern, format, confirm_size_gb)` creates a guarded archive
    from matched files.
- **Marina Hub proxy methods** for all harvest/download operations.
- **MCP tools**: `harvest_inventory`, `harvest`, `download`, `download_glob`.
  `harvest` and `download_glob` return archive metadata by default; callers
  must opt into `include_content=true` to push base64 archive bytes through MCP.
- **Safety defaults**:
  - `essential` mode includes scripts, logs, common input/output text files,
    and skips non-essential/large files.
  - Generated harvest archives are excluded from subsequent harvests.
  - `download_glob` hard-fails above 5 GB unless `confirm_size_gb` covers the
    matched total.

### Tested

- `ruff check .`
- `mypy src tests`
- `pytest -q` → 210 passed

## [Unreleased] — M2-B: watchdog system + events fan-out (2026-05-05)

Watchdog framework, three default checks, gRPC RPCs for events, Marina
fan-out, and an end-to-end L3 test on a real Docker container that forces
a `disk_low` fire and observes it through Marina. The "save my night"
foundation.

### Added

- **`lotsman/watchdogs/` package**:
  - `base.py` — `Check` Protocol, `CheckResult` (severity: notify | kill |
    checkpoint), `WatchdogContext` (frozen, read-only view of a job's
    state on each tick).
  - `checks.py` — three production-ready checks:
    - `DiskLowCheck` (default 5 GB threshold; s126 lesson)
    - `ProcessExitOomCheck` (post-mortem fire on exit codes 9 / 137)
    - `GpuIdleCheck` (default 30-min window at <5% utilization;
      `nvidia-smi` shell-out, no-op without it; cost rationale: idle
      A100 = $0.70/hr drain)
  - `supervisor.py` — thread-safe `Supervisor` with `register/unregister/
    list_watchdogs/history/all_history/fired_names/tick/start/stop/
    add_listener`. Fires at most once per check (idempotent — first
    crossing wins). Buggy checks and listeners can't break the loop.

- **gRPC RPCs** (proto bumped, stubs regenerated):
  - `Events(EventsRequest) returns (stream Event)` — server-streaming
    live + replay. `since_unix_ms > 0` replays history; client cancellation
    cleans up subscription.
  - `WatchdogList(WatchdogListRequest)` — current watchdog set + fired
    status per check.
  - `WatchdogHistory(WatchdogHistoryRequest)` — past fired events for a
    job, with optional `since_unix_ms` cutoff.
  - `EventsHistoryAll(EventsHistoryAllRequest)` — all events across every
    job on this Lotsman, sorted by time. Marina uses it for fleet fan-out
    in one round-trip per host.

- **LotsmanService wiring**:
  - Each instance owns a Supervisor + per-job `_event_log` + listener.
  - `default_checks` / `default_checks_factory` ctor knobs choose the
    auto-attached set. Production default = the three checks above.
  - `Run` registers the default check set under the new jobId before
    returning. Supervisor.start at __init__; shutdown stops it cleanly.
  - Watchdog tunables read from env at `lotsman serve` startup:
    `LOTSMAN_DISK_LOW_GB`, `LOTSMAN_DISK_LOW_INTERVAL_S`,
    `LOTSMAN_GPU_IDLE_PCT`, `LOTSMAN_GPU_IDLE_SECONDS`,
    `LOTSMAN_GPU_IDLE_INTERVAL_S`. Admin can tune per-host without
    rebuilding the image; tests force-fire via env override.

- **Sea / Hub**:
  - `Sea` Protocol gains `env: dict[str, str] | None` on `create()`.
    DockerSea appends `-e KEY=VAL` flags. Hub.host_create propagates.
  - `Hub.watchdog_list / watchdog_history` route per-job by jobId prefix.
  - `Hub.events(jobId)` — alias of watchdog_history.
  - `Hub.events_all(since=0, hosts=None)` — fan-out across registered
    hosts via EventsHistoryAll. A failed RPC contributes []
    (observability, not transactional).

- **MCP tools**: `watchdog_list`, `watchdog_history`, `events`,
  `events_all`. `events_all` takes `hosts="a,b"` comma-string for filter
  (or empty = all).

- **`scripts/marina.example.toml`** — annotated config example with
  `[hosts.*]` and `[seas.*]` blocks.

### Tested

- 49 new tests (146 → 195 total, ruff clean, ~84 s wall incl. one
  Docker rebuild for L3 watchdog smoke):
  - 27 L1 unit (`test_watchdogs_checks` 14 + `test_watchdogs_supervisor`
    13)
  - 9 L2 service for new RPCs (`test_events_basic`)
  - 7 L2 hub fan-out (`test_marina_events_hub` — two-Lotsman
    aggregation, host filter, since filter, dead-host swallow)
  - 4 MCP face expansions
  - 1 L2 docker subprocess (env passthrough)
  - 2 L3 integration (`test_watchdogs_real`):
    - Defaults attached after `Run`
    - `disk_low` fires within 10 s when forced via env, observed via
      Marina `events_all` → `EventsHistoryAll` against a real container.
      This is the same path Claude Code uses overnight to spot zombies.

### Deferred (upstream blockers)

- **Tier 2 — native MCP Tasks API.** `mcp` Python SDK 1.26.0 ships the
  Task *types* (`Task`, `TaskStatus`, `tasks/get`, `TaskStatusNotification`)
  but neither `FastMCP` nor lowlevel `Server` surfaces task **handlers**.
  Implementing requires raw protocol-handler registration plus uncertain
  client-side support (does Claude Code 2.1.x actually call `tasks/get`
  on a tool result tagged as a Task? unknown). Revisit when SDK exposes
  task handlers natively.

- **Tier 3 — `claude/channel` push (real-time wake-from-sleep).**
  Undocumented in standard MCP; Claude Code-specific capability with
  the design-doc caveat that behavior may have shifted between releases.
  Standard MCP `LoggingMessageNotification` and `ProgressNotification`
  are available but only flow within an active session — they don't
  wake a sleeping Claude Code.

  In the meantime: **Tier 1 polling (this release) is fully sufficient**
  for the `/loop` + `ScheduleWakeup` pattern Claude Code already uses on
  this project — every wakeup calls `events_all(since=last_check)` and
  reacts. The "save my night" goal is met; real-time push would just
  reduce wake latency from ~30 min to ~seconds.

## [Unreleased] — M2-A: sea abstraction + DockerSea (2026-05-05)

Provider-agnostic hosting. Adds a `Sea` abstraction (gomer / loki / vast /
runpod / ...) so end-to-end provisioning workflows can be tested on free
local Docker before any cloud burn.

### Added

- **`marina/seas/` package** — provider abstraction layer:
  - `base.py` — `Sea` Protocol (search/recommend/create/destroy/stop/start/
    cost_summary/status/list_instances/renew) + `Offer`, `HostHandle`,
    `CostBreakdown`, `SeaStatus` frozen dataclasses
  - `presets.py` — 4 workload presets (`dft_paper_grade`, `dft_smoke`, `mlip`,
    `aimd_long`) encoding project lessons (FP64 only, GHz≥5.0, reliability
    floors). `matches(offer, preset)` is pure boolean check.
  - `registry.py` — module-level `register_sea / get_sea / list_seas`
    (test helper; production goes through `Hub`)
  - `factory.py` — `build_sea(name, type, raw)` from a TOML section,
    dispatching on `type` (`docker_sea` for now; `vast_sea` in M2-B)
  - `runner.py` — injectable `Runner` Protocol + `subprocess_runner` default,
    so DockerSea is unit-testable without spawning real `docker`
  - `docker_sea.py` — `DockerSea` over `docker --context <ctx>`. One container
    = one host. `--gpus all` only when capability declares a GPU. Auto-names
    containers `{sea}-{N}`, parses random host port via `docker inspect`.
    `reliability` defaults to 1.0 (owner-attested) so owned A100 boxes pass
    `dft_paper_grade`; admin can lower it in config.

- **MCP API** in `marina/mcp_server.py`:
  - sea queries: `sea_list`, `sea_search`, `sea_recommend`, `sea_status`,
    `cost_summary` (per-sea or aggregated)
  - host lifecycle: `host_create(sea, image, ...)`, `host_add` (manual /
    pre-baked), `host_destroy(name, kill_running?)`, `host_stop`, `host_start`,
    `host_list(sea?)`
  - per-job (unchanged from M1): `run`, `status`, `kill`, `logs`, `whoami`
  - **Renamed**: `host_remove` → `host_destroy`. Sea-managed hosts are torn
    down through their owning Sea (`docker rm` / `vastai destroy`); manual
    hosts just close their gRPC channel.

- **Hub (`marina/hub.py`)**: sea registry + per-host gRPC pool. `host_create`
  delegates to a Sea; the resulting `HostHandle.grpc_target` is wired into
  the channel pool. `HostEntry.sea` tags ownership for `host_destroy`
  dispatch. `cost_summary()` aggregates across seas when called without a
  `sea` argument.

- **Config (`marina/config.py`)**: parses `[seas.NAME]` sections alongside
  existing `[hosts.NAME]`. Seas auto-registered at `marina serve` startup.

- **Tests** — 80 new (66 → 146 total, ≈6.3 s wall):
  - 51 L1 unit (`test_seas_base / _presets / _registry / _docker_pure /
    _factory`, plus `test_marina_config` extensions)
  - 29 L2 service (`test_seas_docker_subprocess` with `FakeRunner`;
    `test_marina_hub_seas` with `FakeSea`; `test_marina_mcp_server` extensions
    covering sea_* / host_create dispatch through the MCP layer)

### Design notes

- "Sea = named instance" was chosen over "sea = type" so users see their
  registered names in `sea_list()` (e.g. `gomer`, `vast_main`, `vast_grant`)
  rather than generic types. Two Vast.ai accounts can coexist as separate
  seas with the same `type=vast_sea`.
- `host_*` is the unified namespace for lifecycle. There is no `sea_create`
  ("create a sea?") / `sea_destroy` — only `host_create(sea=...)`.
- `host_add` survives as a manual escape hatch (pre-baked Lotsman, legacy
  ssh box) — Marina just registers a gRPC endpoint and forgets.

### Deferred to M2-B

- L3 integration smoke against a real local Docker daemon
  (`tests/integration/test_seas_docker_real.py`). Needs `docker build .` of
  `lotsman:latest` first; better as a standalone session.
- `VastSea` impl (search/recommend/create/destroy via `vastai-python`,
  handoff-staleness guard on destroy).
- Watchdog defaults (`gpu_idle`, `scf_plateau`, `disk_low`,
  `cons_qty_drift`, `oom`).
- `claude/channel` push prototype.
- MCP Tasks API integration.

## [Unreleased] — M1 baseline (2026-05-05)

First end-to-end working baseline. Built test-first across 7 commits in a
single design+build session.

### Added

- **gRPC service contract** at `proto/lotsman/v1/lotsman.proto`. Six unary
  and streaming RPCs:
  - `Run` — launch a script under daemon supervision (returns `<host>/<ulid>` jobId)
  - `Status` — poll a job's lifecycle state (PENDING/RUNNING/DONE/FAILED/KILLED)
  - `Kill` — graceful SIGTERM + grace + SIGKILL, idempotent on terminal jobs
  - `Logs` — snapshot stdout/stderr with optional `tail_lines` and stderr inclusion
  - `TailFollow` — server-streaming live tail with byte-offset resume
  - `Whoami` — daemon self-description via `/etc/lotsman/manifest.toml`

- **Lotsman daemon** (in-container service)
  - `lotsman serve` console command
  - Sanitization of incoming scripts: em-dash → `--`, CRLF → LF
  - `resolve_bash` helper that bypasses Windows WSL App Execution Aliases via
    `shutil.which`
  - One-running-job-per-Lotsman semantics; second `Run` while another is
    RUNNING returns `FAILED_PRECONDITION`. (Concurrent jobs deferred — KISS.)
  - Persistent stdout/stderr to `<jobs_dir>/<ulid>/stdout.log` etc.
  - Manifest parser for tool/version/image/defaults/known_pitfalls

- **Marina hub** (local proxy / orchestrator)
  - `marina serve` console command, MCP-over-stdio
  - `marina/router.py`: `parse_job_id(host/ulid) -> (host, ulid)`
  - `marina/hub.py`: gRPC channel pool keyed by host name; routes per-job
    RPCs by jobId prefix
  - `marina/mcp_server.py`: FastMCP façade exposing 8 tools (host_add,
    host_remove, host_list, run, status, kill, logs, whoami)
  - `marina/config.py`: TOML loader for `~/.lotsman/marina.toml` with
    `[hosts.NAME]` sections; auto-registers at startup

- **Standalone Docker image** (`Dockerfile`) on `python:3.13-slim`. Builds
  to ~250 MB, exposes port 50051, ships both `lotsman` and `marina`
  console commands. `.dockerignore` controls image size.

- **Tests** — 66 total, <6s wall:
  - 13 L1 unit (sanitize, tail_bytes, manifest, marina config, marina router)
  - 48 L2 service (gRPC in-process, real bash subprocess; covers all 6 RPCs,
    Hub cross-routing across two Lotsman instances, MCP factory)
  - 5 L3 integration (subprocess daemons via `python -m`, real CLI args,
    real gRPC ping, real Run+Status flow)

### Validated

- **End-to-end smoke on remote Linux Docker** (gomer): `docker --context
  gomer build` → `docker run` → `docker exec python smoke.py` →
  Whoami + Run + Status + Logs all green.

### Architecture decisions

See parent project's `knowledge/DECISIONS.md`:
- РЕШЕНИЕ-084 — initial sub-project creation
- РЕШЕНИЕ-085 — Marina-Lotsman split (single MCP entry point, dynamic fleet)
- РЕШЕНИЕ-086 — gRPC for Marina↔Lotsman (not MCP), TDD discipline mandatory

### Deferred (M2+)

- SSH-tunneled UDS transport for Marina↔Lotsman (currently plain TCP)
- Connection management: auto-reconnect, heartbeat, lost-host detection
- Vast.ai control plane in Marina (`vast_search` / `vast_create` / `vast_destroy`)
- Watchdog defaults (gpu_idle, scf_plateau, disk_low, cons_qty_drift, oom)
- `claude/channel` push for real-time alerts
- Per-tool image layering: Lotsman bolted onto `infra-qe-gpu` etc.
- `Harvest` streaming RPC (essential / full / debug modes with size guard)
- External webhooks (Slack/Telegram/email/PagerDuty)
