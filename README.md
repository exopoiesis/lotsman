# Lotsman

> An in-image MCP server that boards your compute container and pilots it through
> the local hazards — MSYS path traps, em-dash crashes, mpirun silent-fails,
> SIGTERM-10s drills, ENVIRON bulk rejection, ASE quirks, and the rest.

**Status:** M2 filesystem + harvest complete (2026-06-02). 20 gRPC RPCs,
210 tests, two daemon CLIs, Dockerfile validated on remote Linux Docker.
See [`CHANGELOG.md`](CHANGELOG.md) and [`docs/DESIGN.md`](docs/DESIGN.md).

## Quick start

```bash
# Install (development)
git clone https://github.com/exopoiesis/lotsman
cd lotsman
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e .[dev]

# Generate proto stubs (one-time)
python -m grpc_tools.protoc -I proto --python_out=src --grpc_python_out=src \
    proto/lotsman/v1/lotsman.proto

# Run tests (210)
pytest

# Run a Lotsman daemon (in a container or locally for testing)
lotsman serve --port 50051 --host-id local --jobs-dir /tmp/lotsman/jobs

# In another shell — write Marina config
mkdir -p ~/.lotsman
cat > ~/.lotsman/marina.toml <<EOF
[hosts.local]
target = "localhost:50051"
EOF

# Run Marina (MCP-over-stdio for Claude Code etc.)
marina serve --config ~/.lotsman/marina.toml
```

Docker validation:
```bash
docker build -t lotsman:smoke .
docker run -d --name lotsman-test lotsman:smoke
docker cp scripts/docker_smoke.py lotsman-test:/tmp/smoke.py
docker exec lotsman-test python /tmp/smoke.py
```

## Instance Scout

`lotsman scout` is a quick hardware acceptance test for a fresh compute
instance. It is not a QE/CP2K/ABACUS smoke run; it fingerprints the machine so
you can compare instance types before burning money on production calculations.

Basic run inside a compute container:

```bash
lotsman scout run --workspace /workspace --out /workspace/scout.json
```

Useful short run while developing locally:

```bash
lotsman scout run \
  --workspace /tmp/lotsman-scout \
  --out /tmp/scout.json \
  --fio-size-mb 256 \
  --fio-runtime-s 5 \
  --pretty
```

The JSON report includes:

| Probe | What it captures |
|---|---|
| Inventory | `cpu.max`, platform, `lscpu`, RAM, workspace disk, `nvidia-smi`, GPU topology |
| `fio` | Sequential read/write and mixed random 4k I/O on the selected workspace |
| STREAM | CPU memory bandwidth, if a `stream`, `stream_c`, or `stream.omp` binary exists |
| `nvidia-smi dmon` | Short GPU utilization/power/clock/memory telemetry sample |
| `nvbandwidth` | GPU memory, PCIe, and GPU-GPU bandwidth, if installed |
| DCGM | Optional diagnostics via `--dcgm`, if `dcgmi` works in the image |

DCGM can be useful on well-prepared NVIDIA images, but it may require host
services or permissions. Keep it opt-in:

```bash
lotsman scout run --workspace /workspace --out /workspace/scout-dcgm.json --dcgm
```

The base Lotsman Dockerfile installs `fio` and standard Linux inventory/build
tools. CUDA-derived images can also build optional NVIDIA probes:

```bash
bash src/lotsman/scout/install_gpu_tools.sh
```

That script builds `nvbandwidth` when `nvcc` is available and builds
`nccl-tests` when NCCL headers are present. Missing CUDA/NCCL is treated as a
clean skip so the same layer can be reused across CPU-only and GPU images.

## Why

Running scientific compute workloads on ephemeral cloud GPU instances
(Vast.ai, RunPod, Lambda, etc.) is full of *known-but-easy-to-forget* traps:

- QE GPU silently crashes without `mpirun` even at `np=1`.
- `scp -r` over Vast.ai SSH-relay drains hours on multi-GB wavefunction files.
- Em-dash `—` in a script crashes ASCII-only containers.
- Windows MSYS rewrites `/tmp/` mid-flight, breaking `docker cp`/`scp` args.
- `pgrep -f` self-matches its own subshell — kill returns success, process lives.
- Restarting a CP2K run with a new `PROJECT` name races on shared `output.log`.
- `disk_io='low'` in QE breaks SIGTERM recovery on Vast.ai's 10-second grace.

Each pitfall costs at least one sleepless night. We've catalogued ~40 of them.
Lotsman bakes the catalogue into a single binary that ships in every compute
image and exposes a clean MCP API to the orchestrator (Claude Code, in our case).

## Metaphor

A *lotsman* (лоцман) is a local pilot who boards a foreign ship and guides it
through hazardous waters they know intimately. Each Lotsman in each compute
image knows its own waters — the tool's quirks, the platform's traps, the
instance's resources.

## Architecture (60 sec)

Two binaries, one project:

```
┌──────────────┐                  ┌──────────────────────┐
│ Claude Code  │ ◀──MCP stdio──▶ │ Marina               │
│ Codex /      │                  │ (local daemon)       │
│ custom       │                  │                      │
│ orchestrator │                  │ • host registry      │
│              │                  │ • Vast.ai control    │
│ ONE MCP      │                  │ • event aggregation  │
│ entry in     │                  │ • SSH keys & secrets │
│ mcp.json     │                  │                      │
└──────────────┘                  └──────────┬───────────┘
                                             │ gRPC over SSH-forwarded Unix domain socket
                                             │ (persistent connection per host, HTTP/2 multiplexed)
                            ┌────────────────┼────────────────┐
                            ▼                ▼                ▼
                    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
                    │ Lotsman QE   │ │ Lotsman CP2K │ │ Lotsman ABACUS│
                    │ vast/W3      │ │ vast/W1      │ │ gomer        │
                    │ ephemeral    │ │ ephemeral    │ │              │
                    └──────────────┘ └──────────────┘ └──────────────┘
```

**Two protocols, by design:**
- **MCP** for Claude Code ↔ Marina — AI-facing, multiple agent clients, schema discovery via `tools/list`, `claude/channel` push notifications.
- **gRPC** for Marina ↔ Lotsman — service-to-service, native streaming for harvest tarballs and event streams, HTTP/2 multiplexing for high-frequency status polls, protobuf-typed schema evolution.

- **Marina** — local daemon. One MCP entry in `mcp.json`. Routes calls to
  the right Lotsman by jobId. Manages cloud instance lifecycle across
  marketplace seas (Vast.ai, Verda) so Claude doesn't touch the APIs directly.
- **Lotsman** — per-container daemon. Baked into each `infra-<tool>-gpu`
  image. Knows its tool's quirks (manifest.toml). Standalone MCP server —
  can be probed directly for debugging.
- **No Claude Code restart** when adding/removing hosts — Marina handles the
  registry dynamically. Critical for ephemeral cloud workflows.
- Transport: MCP-over-stdio everywhere. SSH key trust between Marina and
  Lotsman; SSH stdio Marina↔Claude Code or stdio direct.
- All Lotsman state ephemeral (jobs JSON in `/var/lotsman/`); container is
  one-shot. Marina state in `~/.lotsman/marina.db` (sqlite).

## Why two binaries

- Adding a new compute host shouldn't require restarting your AI agent.
- A single Vast.ai API key shouldn't be passed through MCP arguments.
- Search filters / DFT-aware presets / cost tracking / pre-destroy
  cleanup live above any single container — they belong to Marina.
- A container's local quirks (mpirun rules, MSYS paths, em-dash sweeps,
  watchdog defaults) belong to its Lotsman.

Each binary is independently usable (Lotsman alone is fine for a single-host
setup), but Marina is the natural orchestration layer once you have more
than one container.

## Core API surface

**Marina-only (host & cloud control plane):**

| Group | Commands |
|---|---|
| Host registry | `host_add`, `host_remove`, `host_list`, `host_status` |
| Marketplace search | `sea_list`, `sea_search`, `seas_search` (all seas at once), `sea_recommend`, `sea_status` |
| Host lifecycle | `host_create`, `host_start`, `host_stop`, `host_destroy`, `host_list` |
| Fleet ops | `kill_all_on_host`, `harvest_all_done`, `events_all`, `cost_summary`, `cost_history` |

Provider-agnostic by design: every command takes a `sea` argument naming the
provider. Marketplace seas today are **`vast`** (Vast.ai) plus two backup
channels — **`verda`** (Verda Cloud — EU regions, native spot) and **`clore`**
(Clore.ai — crypto-settled, consumer-GPU heavy, good for MLIP); owned hardware
(`gomer`, `loki`) is exposed as `docker_sea`s at $0/hr. `sea_search` filters on
`gpu_name` / `vram_gb` / `cpu_name` / `order` and a **`host_type`** dimension —
`any` (default; on-demand + spot merged, so nothing is hidden), `on-demand`, or
`spot` (interruptible; `$/hr` = the spot floor) — and returns a server-rendered
table whose `type` column flags OD vs spot. **`seas_search`** runs the same filter across *all*
marketplace seas at once (top N from each, merged and sorted, with a leading
`sea` column) for apples-to-apples shopping. Two synthetic host-fitness scores
(`zGPU` for FP64 GPU-DFT, `zCPU` for CP2K) rank hosts; see
[`docs/HOST_SCORING.md`](docs/HOST_SCORING.md).

**Per-job (Marina proxies to Lotsman):**

| Group | Commands |
|---|---|
| Lifecycle | `run`, `status`, `wait`, `kill`, `restart`, `list_jobs` |
| Logs | `logs`, `tail_follow`, `progress` (tool-aware), `events` |
| Harvest | `harvest_inventory`, `harvest`, `download`, `download_glob` |
| Filesystem | `upload`, `ls`, `stat`, `cat`, `mkdir`, `disk_free` (`rm` deferred until safe-rm policy) |
| Self-knowledge | `whoami`, `health`, `bench_quick`, `gpu_status`, `processes`, `help`, `examples` |
| Watchdogs | `watchdog_list`, `watchdog_history`, `watchdog_add`, `watchdog_remove` |
| Tool-specific | `prepare_input`, `validate_input`, `pseudopotentials`, `lessons_for` |
| Resilience | `checkpoint_force`, `sigterm_drill` |

Watchdogs auto-attach on `run()` from manifest defaults — no need to
remember. Full spec with parameters, return types, presets, and the
under-the-hood auto-fix layer in [`docs/DESIGN.md`](docs/DESIGN.md).

## Sensitive data

Containers are one-shot, so no long-term secrets live in the server.
`env` dicts in `run()` go through an allow-list filter; logs are scrubbed
for known secret patterns. See [`docs/SECURITY.md`](docs/SECURITY.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE).

## Origin

Spun out of the [Third Matter](https://exopoiesis.space) project after one
too many overnight DFT runs lost to deployment papercuts.
