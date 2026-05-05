# Changelog

All notable changes to Lotsman are documented here. Format is loosely based on
[Keep a Changelog](https://keepachangelog.com/) and the project follows
[Semantic Versioning](https://semver.org/).

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
