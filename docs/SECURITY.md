# Security & Sensitive Data

Lotsman lives inside ephemeral compute containers. Containers are one-shot —
no long-term secret state inside the server itself. The threat model below
reflects that.

## Threat model

- **In scope:**
  - Accidental leak of caller-supplied secrets through `logs()` / `cat()` /
    `events()` returns.
  - Accidental persistence of secrets across `restart()` / `harvest()`.
  - Compromised orchestrator account submitting destructive `run()` payloads —
    we cannot prevent this (server runs as root in the container by design,
    matching tools' requirements), but we can make it auditable.

- **Out of scope:**
  - Long-term key management (containers are ephemeral; if you need
    persistent secrets, mount them).
  - Multi-tenant isolation (one container = one workload = one trust domain).

## Authentication

### Default: stdio over SSH

```
ssh root@<host> docker exec <container> lotsman --stdio
```

- Inherits the host's existing SSH-key trust.
- Zero extra configuration.
- One MCP session per SSH connection — fine for our orchestrator pattern.

### Optional: HTTP/SSE with one-time token

For multi-session use, Lotsman can listen on a Unix domain socket or TCP port
and emit a per-instance one-time token at boot:

```
docker logs <container> | grep "lotsman token:"
# lotsman token: 7bf2a93e1d5c4f8a... (binds to first connection, rotated on restart)
```

- Token is generated at server start, printed once to stderr / `docker logs`.
- First successful auth binds the token; further connections with the same
  token are accepted from the same IP only.
- Token is rotated on every server restart.
- TLS optional; recommended if exposed beyond `localhost` / port-forward.

## `env` dict filtering in `run()`

`env` arguments to `run()` go through an allow-list before being passed to
the spawned process.

**Allowed prefixes** (compute-relevant only):
- `OMP_*`, `MPI_*`, `OPENMPI_*`, `INTEL_MPI_*`
- `CUDA_*`, `NCCL_*`, `NVHPC_*`
- `MKL_*`, `OPENBLAS_*`, `BLAS_*`, `LAPACK_*`
- `*_NUM_THREADS`
- Tool-specific prefixes from `manifest.toml` (e.g., `QE_*`, `CP2K_*`,
  `ABACUS_*`)
- `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH` (logged)

**Blocked** (silently stripped + warning in `events()`):
- `AWS_*`, `GCP_*`, `AZURE_*`, `OCI_*`
- `OPENAI_*`, `ANTHROPIC_*`, `HF_*`, `HUGGINGFACE_*`
- `GH_*`, `GITHUB_*`, `GITLAB_*`
- `*_TOKEN`, `*_SECRET`, `*_KEY`, `*_PASSWORD`, `*_API_KEY`
- `NETRC`, `~/.netrc`-style hints

If you genuinely need a credential inside the workload, mount it via a
file or use a tool-specific secret store — don't pass through `env`.

## Log scrubbing

`logs()`, `tail_follow()`, `cat()`, and `events()` pass output through a
regex scrubber before returning. Patterns matched:

- AWS access keys: `AKIA[0-9A-Z]{16}`, `ASIA[0-9A-Z]{16}`
- GitHub tokens: `gh[pousr]_[A-Za-z0-9]{36,}`
- Anthropic: `sk-ant-[A-Za-z0-9_-]{90,}`
- OpenAI: `sk-[A-Za-z0-9]{48,}`
- HuggingFace: `hf_[A-Za-z0-9]{30,}`
- Generic high-entropy 40+ char tokens preceded by `token`/`secret`/`key`/
  `password`/`bearer` (case-insensitive)
- Common base64-encoded secrets near the same keywords

Matches are replaced with `***REDACTED***`. The scrubber is defence-in-depth,
not a guarantee — don't put secrets in stdout in the first place.

## Optional secrets file

If a tool legitimately needs persistent credentials (commercial license keys,
private dataset URLs, etc.), mount them via volume:

```
/etc/lotsman/secrets.d/<name>.toml
```

- Volume-mounted, never baked into the image.
- Referenced by name from `manifest.toml`.
- Visible only to processes spawned by Lotsman; not exposed through API.

## Audit

Every `run()` / `kill()` / `harvest()` / `download_glob()` call writes to
`/var/lotsman/audit.log` with timestamp, caller (auth source), and parameters
(secrets-scrubbed). Survives within the container lifetime; harvested with
`mode=debug`.

## Reporting issues

Until we have a public contact, file via the project's GitHub issues with
the `security` label. For sensitive reports, email igor@exopoiesis.space.
