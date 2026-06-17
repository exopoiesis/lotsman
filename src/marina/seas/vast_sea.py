"""VastSea — a hosting sea backed by the `vastai` CLI (Vast.ai REST API).

Unlike DockerSea (one static, owner-attested offer), Vast.ai is a *marketplace*:
`search()` returns many dynamic offers, `create()` rents one of them, and the
host runs until explicitly destroyed (no lease to renew for on-demand).

All Vast.ai access goes through the same injectable `Runner` used by DockerSea,
so unit/service tests script `vastai ... --raw` responses without hitting the
network. The API key is resolved from an env var (`api_key_env`, default
`VAST_API_KEY`) and passed via `--api-key`; it lives only inside Marina and is
never surfaced through the MCP boundary (mirrors DockerSea passing `-e KEY=VAL`).

Scope (M2-B): search / recommend / create / list / destroy / stop / start /
status / balance / cost. SSH→gRPC tunnel wiring (so Marina can actually reach
the in-container Lotsman) is a higher-layer M3 concern; `create()` returns the
`ssh_target` and a provisional `grpc_target` for that layer to forward.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable

from marina.seas.base import CostBreakdown, HostHandle, Offer, SeaStatus
from marina.seas.forwarding import Forward, Forwarder, SubprocessForwarder
from marina.seas.perf_score import zcpu, zgpu
from marina.seas.presets import PRESETS, matches
from marina.seas.runner import Runner, RunResult, subprocess_runner


class VastSeaError(Exception):
    """Raised when a `vastai` command fails or returns unusable output."""


# GPU families with usable native FP64 (project rule: paper-grade DFT only on
# these). Matched as a case-insensitive substring of the Vast `gpu_name`.
_FP64_GPU_TOKENS = (
    "A100", "A800", "H100", "H800", "H200", "GH200", "GB200",
    "B100", "B200", "V100", "P100",
)

# Vast lists most accelerators by sub-model ("A100 SXM4", "Tesla V100"), so a
# bare family name like "A100" never matches `gpu_name=A100`. Map common
# families to their real Vast tokens so a user can search by family. Verified
# live against the marketplace; an unknown gpu_name falls through to an exact
# match, and a substring fallback in search() guards an uncatalogued variant.
_GPU_FAMILIES: dict[str, tuple[str, ...]] = {
    "A100": ("A100 PCIE", "A100 SXM4"),
    "A800": ("A800 PCIE", "A800 SXM4"),
    "H100": ("H100 NVL", "H100 PCIE", "H100 SXM"),
    "H200": ("H200", "H200 NVL"),
    "V100": ("Tesla V100",),
    "P100": ("Tesla P100",),
    "B200": ("B200",),
}

# CPU families. Unlike gpu_name, Vast does not expose cpu_name as a queryable
# field, so these are matched python-side as case-insensitive substrings of the
# offer's cpu_name. A family expands to several patterns (newest gen first); an
# unknown value is treated as a single literal substring (e.g. "5955WX").
_CPU_FAMILIES: dict[str, tuple[str, ...]] = {
    # AMD Threadripper PRO WX workstation chips — the high-clock family the
    # project settled on for CP2K/DFT (e.g. 5955WX ~7 GHz). Gens 7 > 5 > 3.
    "TRPRO": ("Threadripper PRO 7", "Threadripper PRO 5", "Threadripper PRO 3"),
    "TR_PRO": ("Threadripper PRO 7", "Threadripper PRO 5", "Threadripper PRO 3"),
    "THREADRIPPER_PRO": (
        "Threadripper PRO 7", "Threadripper PRO 5", "Threadripper PRO 3",
    ),
}

# Offer attribute each sort key maps to (used by search(order=...)).
_ORDER_KEYS = {
    "cpu_ghz": "cpu_ghz", "ghz": "cpu_ghz",
    "price": "price_per_hour", "dph": "price_per_hour",
    "dph_total": "price_per_hour", "price_per_hour": "price_per_hour",
    "vram": "vram_gb", "vram_gb": "vram_gb",
    "cores": "cpu_cores", "cpu_cores": "cpu_cores",
    "ram": "ram_gb", "ram_gb": "ram_gb",
    "disk": "disk_gb", "disk_gb": "disk_gb",
    "reliability": "reliability",
    "dlperf": "dlperf",
    "dlpd": "dlperf_per_dollar", "dlperf_per_dollar": "dlperf_per_dollar",
    "perf_per_dollar": "dlperf_per_dollar",
    "zcpu": "zcpu", "zgpu": "zgpu",
    "gpu_mem_bw": "gpu_mem_bw_gbs", "pcie_bw": "pcie_bw_gbs",
    "cuda": "cuda_max_good", "cuda_max_good": "cuda_max_good",
}

# When python-side filtering/sorting is requested we fetch a broad pool, not
# just the cheapest `limit` — otherwise a perf sort (e.g. -zgpu) would never see
# premium GPUs (A100/H100) that are priced above the cheapest N. Large enough to
# cover the whole verified market; when a server-side gpu_name filter is active
# the query already narrows the result, so this stays cheap in practice.
_SEARCH_FETCH_CAP = 2000

# Vast `actual_status` → our HostHandle.state.
_STATE_MAP = {
    "running": "running",
    "loading": "loading",
    "created": "loading",
    "scheduling": "loading",
    "exited": "stopped",
    "stopped": "stopped",
    "offline": "stopped",
}

# Statuses that mean the instance will never come up on its own.
_TERMINAL_BAD = {"exited", "offline", "error"}

# Substrings in a Vast instance `status_msg` that mean it will never reach
# `running` on its own (bad image, registry auth, out of disk). A failed image
# pull keeps `actual_status` at "loading"/"created" while the daemon retries
# forever, so without this the wait loop would block until the full timeout.
_FATAL_STATUS_MARKERS = (
    "pull access denied",
    "repository does not exist",
    "error response from daemon",
    "manifest unknown",
    "manifest for",          # "manifest for <img> not found"
    "denied:",
    "no space left",
    "invalid reference format",
    "unauthorized",
)

_DEFAULT_READY_TIMEOUT_S = 1800.0  # 30 min for the instance to reach `running`
_DEFAULT_POLL_INTERVAL_S = 10.0
_DEFAULT_SSH_READY_TIMEOUT_S = 180.0  # sshd usually up within ~1-2 min of running
_DEFAULT_CONTAINER_GRPC_PORT = 50051  # where `lotsman serve` listens in-image
# Hard ceiling on any single `vastai` invocation. A search returns in ~2 s; this
# only fires if the CLI wedges (bad network, prompt) — turning an indefinite
# hang of the whole MCP server into a fast, explicit error.
_DEFAULT_CMD_TIMEOUT_S = 45.0


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]  # tolerate "8.0"/8.0/8
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def fp64_native(gpu_name: str) -> bool:
    """True iff the GPU model has usable native double-precision."""
    upper = gpu_name.upper()
    return any(tok in upper for tok in _FP64_GPU_TOKENS)


def _default_pubkey_loader(path: str) -> str:
    """Read an SSH public key file from the local Marina host."""
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return f.read().strip()


class VastSea:
    """A hosting sea backed by `vastai` (the Vast.ai marketplace CLI)."""

    def __init__(
        self,
        name: str,
        *,
        api_key: str | None = None,
        api_key_env: str = "VAST_API_KEY",
        runner: Runner | None = None,
        forwarder: Forwarder | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        ssh_user: str = "root",
        ssh_key_path: str | None = None,
        ssh_pubkey_path: str | None = None,
        pubkey_loader: Callable[[str], str] | None = None,
        container_grpc_port: int = _DEFAULT_CONTAINER_GRPC_PORT,
        ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        ssh_ready_timeout_s: float = _DEFAULT_SSH_READY_TIMEOUT_S,
        vastai_bin: str = "vastai",
        cmd_timeout_s: float = _DEFAULT_CMD_TIMEOUT_S,
    ) -> None:
        self.name = name
        self.api_key_env = api_key_env
        self._api_key = api_key if api_key is not None else os.environ.get(api_key_env)
        # Executable to invoke. Default relies on PATH; pin to a full path
        # (e.g. ".../Scripts/vastai.exe") when the Marina process PATH omits it.
        self._vastai_bin = vastai_bin
        self._cmd_timeout_s = cmd_timeout_s
        self._runner: Runner = runner if runner is not None else subprocess_runner
        self._forwarder: Forwarder = (
            forwarder if forwarder is not None else SubprocessForwarder()
        )
        self._clock: Callable[[], float] = clock or time.time
        self._sleeper: Callable[[float], None] = sleeper or time.sleep
        self._ssh_user = ssh_user
        # Local SSH identity Marina owns (replaces the skypilot relay's key).
        # private key for login/tunnel; public key gets attached to instances.
        self._ssh_key_path = ssh_key_path
        self._ssh_pubkey_path = ssh_pubkey_path
        self._pubkey_loader = pubkey_loader or _default_pubkey_loader
        self._container_grpc_port = container_grpc_port
        self._ready_timeout_s = ready_timeout_s
        self._poll_interval_s = poll_interval_s
        self._ssh_ready_timeout_s = ssh_ready_timeout_s
        # name -> handle; name is Marina-side, also encoded into the Vast label
        # so list_instances() can reconcile across a Marina restart.
        self._hosts: dict[str, HostHandle] = {}
        # name -> live SSH tunnel; its local_port is the gRPC target.
        self._forwards: dict[str, Forward] = {}

    # ----- vastai dispatch helpers -----

    def _vast(self, sub_argv: list[str], *, raw: bool = True) -> RunResult:
        argv = [self._vastai_bin, *sub_argv]
        if raw and "--raw" not in argv:
            argv.append("--raw")
        if self._api_key:
            # Inside Marina only; never crosses the MCP boundary.
            argv += ["--api-key", self._api_key]
        try:
            return self._runner(argv, timeout=self._cmd_timeout_s)
        except subprocess.TimeoutExpired as exc:
            # Never let a wedged CLI hang the whole MCP server indefinitely.
            raise VastSeaError(
                f"vastai {sub_argv[0] if sub_argv else ''} timed out after "
                f"{self._cmd_timeout_s:.0f}s"
            ) from exc
        except FileNotFoundError as exc:
            raise VastSeaError(
                f"vastai executable {self._vastai_bin!r} not found on PATH; "
                f"set `vastai_bin` to its full path in marina.toml"
            ) from exc

    def _vast_json(self, sub_argv: list[str]) -> object:
        if self._api_key is None:
            raise VastSeaError(
                f"sea {self.name!r}: no Vast.ai API key "
                f"(set ${self.api_key_env} or pass api_key=)"
            )
        result = self._vast(sub_argv)
        if not result.ok:
            # Deliberately omit argv (carries --api-key) from the message.
            raise VastSeaError(
                f"vastai {sub_argv[0] if sub_argv else ''} failed "
                f"(rc={result.returncode}): {result.stderr.strip()}"
            )
        text = result.stdout.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise VastSeaError(f"vastai returned non-JSON output: {exc}") from exc

    def _label(self, host_name: str) -> str:
        # The Vast instance label is just the Marina host name (kept short on
        # purpose). Reconciliation reads it back verbatim; this assumes the
        # account is Marina-managed (no foreign labelled instances to confuse).
        return host_name

    def _host_name_from_label(self, label: object) -> str | None:
        if not isinstance(label, str) or not label.strip():
            return None
        return label.strip()

    # ----- offers / search / recommend -----

    def _offer_from_raw(self, raw: dict[str, object]) -> Offer:
        gpu_name = str(raw.get("gpu_name", "") or "")
        num_gpus = _as_int(raw.get("num_gpus"), 1) or 1
        # Vast reports total host cores/RAM; the rented share is proportional to
        # cpu_cores_effective / cpu_cores when both are present.
        total_cores = _as_int(raw.get("cpu_cores"))
        eff_cores = _as_int(raw.get("cpu_cores_effective"))
        cpu_cores = eff_cores or total_cores
        total_ram_mb = _as_float(raw.get("cpu_ram"))
        if total_cores and eff_cores:
            ram_mb = total_ram_mb * (eff_cores / total_cores)
        else:
            ram_mb = total_ram_mb
        cpu_ghz = _as_float(raw.get("cpu_ghz"))
        cpu_name = str(raw.get("cpu_name", "") or "")
        gpu_mem_bw = _as_float(raw.get("gpu_mem_bw"))  # measured VRAM BW, GB/s
        pcie_bw = _as_float(raw.get("pcie_bw"))        # measured PCIe BW, GB/s
        total_flops = _as_float(raw.get("total_flops"))  # FP32 TFLOPS (Vast)
        return Offer(
            sea=self.name,
            offer_id=str(raw.get("id", "")),
            gpu_model=gpu_name,
            gpu_count=num_gpus,
            vram_gb=_as_int(raw.get("gpu_ram")) // 1024,  # per-GPU MB -> GB
            fp64_native=fp64_native(gpu_name),
            cpu_ghz=cpu_ghz,
            cpu_cores=cpu_cores,
            cpu_name=cpu_name,
            cpu_cores_total=total_cores,
            geolocation=str(raw.get("geolocation", "") or ""),
            dlperf=_as_float(raw.get("dlperf")),
            dlperf_per_dollar=_as_float(raw.get("dlperf_per_dphtotal")),
            gpu_mem_bw_gbs=gpu_mem_bw,
            pcie_bw_gbs=pcie_bw,
            cuda_max_good=_as_float(raw.get("cuda_max_good")),
            zcpu=zcpu(cpu_cores, total_cores, cpu_ghz, cpu_name),
            zgpu=zgpu(
                gpu_name, num_gpus, gpu_mem_bw, pcie_bw, total_flops=total_flops
            ),
            ram_gb=int(ram_mb // 1024),
            disk_gb=_as_int(raw.get("disk_space")),
            price_per_hour=_as_float(raw.get("dph_total")),
            reliability=_as_float(raw.get("reliability2"), None)  # type: ignore[arg-type]
            if raw.get("reliability2") is not None
            else None,
            inet_down_mbps=_as_float(raw.get("inet_down"), None)  # type: ignore[arg-type]
            if raw.get("inet_down") is not None
            else None,
            extras={
                "cuda_max_good": raw.get("cuda_max_good"),
                "verified": raw.get("verified"),
                "machine_id": raw.get("machine_id"),
                "geolocation": raw.get("geolocation"),
            },
        )

    def _build_query(self, filters: dict[str, object] | None) -> str:
        f = dict(filters or {})
        tokens: list[str] = ["rentable=true"]
        if f.get("verified", True):
            tokens.append("verified=true")
        if "num_gpus" in f:
            tokens.append(f"num_gpus={_as_int(f['num_gpus'], 1)}")
        gpu_tok = self._gpu_name_token(f.get("gpu_name"))
        if gpu_tok:
            tokens.append(gpu_tok)
        if "min_reliability" in f and f["min_reliability"] is not None:
            tokens.append(f"reliability>{_as_float(f['min_reliability'])}")
        if "max_dph" in f and f["max_dph"] is not None:
            tokens.append(f"dph_total<{_as_float(f['max_dph'])}")
        if "min_cuda" in f and f["min_cuda"] is not None:
            # cuda_max_good = highest CUDA the host runs well (matches the image
            # we deploy); cuda_vers is only the installed driver — wrong field.
            tokens.append(f"cuda_max_good>={_as_float(f['min_cuda'])}")
        # Raw escape hatch for power users: appended verbatim.
        extra = f.get("query")
        if isinstance(extra, str) and extra.strip():
            tokens.append(extra.strip())
        return " ".join(tokens)

    @staticmethod
    def _gpu_name_token(gpu_name: object) -> str:
        """Vast query token for a GPU name, family-aware.

        ``A100`` -> ``gpu_name in [A100_PCIE,A100_SXM4]``; a specific model
        (``A100 SXM4``) -> exact ``gpu_name=A100_SXM4``; empty -> no token.
        """
        if not gpu_name:
            return ""
        name = str(gpu_name).strip()
        variants = _GPU_FAMILIES.get(name.upper())
        if variants:
            joined = ",".join(v.replace(" ", "_") for v in variants)
            return f"gpu_name in [{joined}]"
        return f"gpu_name={name.replace(' ', '_')}"

    @staticmethod
    def _cpu_patterns(cpu_name: object) -> list[str]:
        """Substring patterns for a cpu_name filter, family-aware.

        ``trpro`` -> the Threadripper PRO 7/5/3 patterns; any other value is a
        single literal substring (e.g. ``5955WX`` or ``EPYC 7763``); empty -> none.
        """
        if not cpu_name:
            return []
        name = str(cpu_name).strip()
        fam = _CPU_FAMILIES.get(name.upper().replace(" ", "_"))
        return list(fam) if fam else [name]

    @staticmethod
    def _sort_offers(offers: list[Offer], order: str) -> list[Offer]:
        """Sort by an Offer field; leading ``-`` means descending."""
        desc = order.startswith("-")
        attr = _ORDER_KEYS.get((order[1:] if desc else order).lower())
        if attr is None:
            raise VastSeaError(
                f"unknown sort key {order!r}; known: {sorted(set(_ORDER_KEYS))}"
            )

        def keyfn(o: Offer) -> float:
            val = getattr(o, attr)
            return float(val) if val is not None else float("-inf")

        return sorted(offers, key=keyfn, reverse=desc)

    def search(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 20,
    ) -> list[Offer]:
        f = dict(filters or {})
        query = self._build_query(f)
        # vram_gb (exact) and order are applied python-side, so they need the
        # full matching pool — fetch the cap, not just the cheapest `limit`.
        vram_gb = _as_int(f["vram_gb"]) if f.get("vram_gb") else 0
        order = str(f.get("order") or "").strip()
        cpu_pats = self._cpu_patterns(f.get("cpu_name"))
        # vram_gb / order / cpu_name are applied python-side, so they need the
        # full matching pool — fetch the cap, not just the cheapest `limit`.
        post = bool(vram_gb or order or cpu_pats)
        fetch_limit = max(limit, _SEARCH_FETCH_CAP) if post else limit
        data = self._vast_json(
            ["search", "offers", query,
             "--order", "dph_total", "--limit", str(fetch_limit)]
        )
        if not isinstance(data, list):
            raise VastSeaError("vastai search offers: expected a JSON array")
        offers = [self._offer_from_raw(o) for o in data if isinstance(o, dict)]
        # Substring guard: if a known family was requested, drop anything Vast
        # returned that doesn't belong to it (defence vs an uncatalogued token).
        fam = str(f.get("gpu_name") or "").strip().upper()
        if fam in _GPU_FAMILIES:
            offers = [o for o in offers if fam in o.gpu_model.upper()]
        if cpu_pats:
            lowered = [p.lower() for p in cpu_pats]
            offers = [
                o for o in offers
                if any(p in o.cpu_name.lower() for p in lowered)
            ]
        if vram_gb:
            offers = [o for o in offers if o.vram_gb == vram_gb]
        if order:
            offers = self._sort_offers(offers, order)
        return offers[:limit]

    def recommend(
        self,
        workload: str,
        budget_per_hour: float | None = None,
        min_hours: int | None = None,
    ) -> list[Offer]:
        del min_hours  # on-demand: no minimum-rental constraint to honour
        if workload not in PRESETS:
            raise ValueError(
                f"unknown workload {workload!r}; known: {sorted(PRESETS)}"
            )
        preset = PRESETS[workload]
        # Push the cheap, expressible constraints into the Vast query; verify
        # the rest (FP64, reliability, disk) locally via matches().
        filters: dict[str, object] = {
            "verified": True,
            "min_reliability": preset.min_reliability,
        }
        if budget_per_hour is not None:
            filters["max_dph"] = budget_per_hour
        offers = self.search(filters=filters, limit=200)
        kept = [o for o in offers if matches(o, preset)]
        if budget_per_hour is not None:
            kept = [o for o in kept if o.price_per_hour <= budget_per_hour]
        kept.sort(key=lambda o: o.price_per_hour)
        return kept

    # ----- lifecycle -----

    def create(
        self,
        image: str,
        *,
        offer_id: str | None = None,
        name: str | None = None,
        disk_gb: int | None = None,
        onstart: str | None = None,
        env: dict[str, str] | None = None,
    ) -> HostHandle:
        if not offer_id:
            raise VastSeaError(
                "vast create requires offer_id (from search/recommend)"
            )
        host_name = name or self._auto_name()
        if host_name in self._hosts:
            raise VastSeaError(
                f"host {host_name!r} already exists in sea {self.name!r}"
            )

        argv = [
            "create", "instance", str(offer_id),
            "--image", image,
            "--ssh",  # expose SSH on the instance (required for our tunnel)
            "--label", self._label(host_name),
        ]
        if disk_gb is not None:
            argv += ["--disk", str(disk_gb)]
        if onstart:
            argv += ["--onstart-cmd", onstart]
        if env:
            env_str = " ".join(f"-e {k}={v}" for k, v in env.items())
            argv += ["--env", env_str]

        created = self._vast_json(argv)
        contract_id = self._extract_contract_id(created)

        # Attach Marina's own public key to the instance, so SSH works without
        # the old skypilot relay holding the key. No-op if no local key is
        # configured (then we rely on keys already on the Vast account).
        self._attach_ssh_key(contract_id)

        entry = self._wait_until_ready(contract_id)
        # Open the SSH tunnel BEFORE building the handle, so _handle_from_instance
        # reports the forwarded local port as the gRPC target.
        self._open_forward(host_name, entry)
        handle = self._handle_from_instance(host_name, entry)
        self._hosts[host_name] = handle
        return handle

    def _attach_ssh_key(self, contract_id: str) -> None:
        """Register Marina's local public key with the instance via `vastai`.

        Self-contained key management: the key lives on the Marina host, not in
        a shared skypilot container. Skipped when `ssh_key_path` is unset (then
        instances inherit whatever keys are attached to the Vast account).
        """
        if not self._ssh_key_path:
            return
        pub_path = self._ssh_pubkey_path or f"{self._ssh_key_path}.pub"
        pubkey = self._pubkey_loader(pub_path).strip()
        if not pubkey:
            raise VastSeaError(f"empty ssh public key at {pub_path!r}")
        result = self._vast(["attach", "ssh", str(contract_id), pubkey])
        if not result.ok:
            raise VastSeaError(
                f"vast attach ssh failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )

    def _open_forward(self, host_name: str, entry: dict[str, object]) -> None:
        """Wait for sshd, then establish the local->container gRPC tunnel."""
        ssh_host = str(entry.get("ssh_host") or "")
        ssh_port = _as_int(entry.get("ssh_port"))
        if not ssh_host or not ssh_port:
            raise VastSeaError(
                f"vast instance {entry.get('id')} is running but exposes no "
                f"ssh_host/ssh_port yet"
            )
        self._wait_ssh_ready(ssh_host, ssh_port)
        # Replace any stale tunnel for this name (e.g. on restart).
        old = self._forwards.pop(host_name, None)
        if old is not None:
            old.close()
        self._forwards[host_name] = self._forwarder.open(
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            remote_port=self._container_grpc_port,
            ssh_user=self._ssh_user,
            identity_file=self._ssh_key_path,
        )

    def _wait_ssh_ready(self, ssh_host: str, ssh_port: int) -> None:
        """Poll `ssh ... true` until sshd accepts a key-based login or we time out."""
        argv = [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",  # never block on a password prompt
            "-o", "ConnectTimeout=10",
            "-p", str(ssh_port),
        ]
        if self._ssh_key_path:
            argv += ["-i", self._ssh_key_path]
        argv += [f"{self._ssh_user}@{ssh_host}", "true"]

        deadline = self._clock() + self._ssh_ready_timeout_s
        last_err = ""
        while True:
            result = self._runner(argv)
            if result.ok:
                return
            last_err = result.stderr.strip()
            if self._clock() >= deadline:
                raise VastSeaError(
                    f"ssh to {ssh_host}:{ssh_port} not ready after "
                    f"{self._ssh_ready_timeout_s:.0f}s: {last_err or 'unreachable'}"
                )
            self._sleeper(self._poll_interval_s)

    def _extract_contract_id(self, created: object) -> str:
        if isinstance(created, dict):
            if not created.get("success", True):
                raise VastSeaError(
                    f"vast create rejected: {created.get('error') or created}"
                )
            cid = created.get("new_contract") or created.get("id")
            if cid is not None:
                return str(cid)
        raise VastSeaError(f"vast create: could not find contract id in {created!r}")

    def _wait_until_ready(self, contract_id: str) -> dict[str, object]:
        deadline = self._clock() + self._ready_timeout_s
        while True:
            entry = self._show_instance(contract_id)
            status = str(entry.get("actual_status") or "").lower() if entry else ""
            if status == "running" and entry is not None:
                return entry
            if status in _TERMINAL_BAD:
                raise VastSeaError(
                    f"vast instance {contract_id} entered {status!r} "
                    f"before becoming ready"
                )
            fatal = self._fatal_status_msg(entry) if entry else None
            if fatal:
                # Fail fast (e.g. bad image / registry auth) instead of polling
                # until the timeout while the daemon retries a doomed pull.
                raise VastSeaError(
                    f"vast instance {contract_id} cannot start: {fatal[:200]}"
                )
            if self._clock() >= deadline:
                raise VastSeaError(
                    f"vast instance {contract_id} not running after "
                    f"{self._ready_timeout_s:.0f}s (last status: {status or 'unknown'})"
                )
            self._sleeper(self._poll_interval_s)

    @staticmethod
    def _fatal_status_msg(entry: dict[str, object]) -> str | None:
        """Return the instance's status_msg iff it signals a doomed startup."""
        msg = str(entry.get("status_msg") or "")
        low = msg.lower()
        if any(m in low for m in _FATAL_STATUS_MARKERS):
            return msg.strip()
        return None

    def _show_instance(self, contract_id: str) -> dict[str, object] | None:
        for entry in self._all_instances():
            if str(entry.get("id")) == str(contract_id):
                return entry
        return None

    def _all_instances(self) -> list[dict[str, object]]:
        data = self._vast_json(["show", "instances"])
        if not isinstance(data, list):
            raise VastSeaError("vastai show instances: expected a JSON array")
        return [e for e in data if isinstance(e, dict)]

    def _handle_from_instance(
        self, host_name: str, entry: dict[str, object]
    ) -> HostHandle:
        ssh_host = entry.get("ssh_host")
        ssh_port = _as_int(entry.get("ssh_port"))
        ssh_target = (
            f"{self._ssh_user}@{ssh_host}:{ssh_port}"
            if ssh_host and ssh_port
            else None
        )
        status = str(entry.get("actual_status") or "").lower()
        # gRPC target = the local end of our SSH tunnel, if one is open for this
        # host. Otherwise (e.g. an unforwarded host seen during reconciliation)
        # fall back to the bare ssh endpoint as a placeholder.
        forward = self._forwards.get(host_name)
        if forward is not None:
            grpc_target = f"127.0.0.1:{forward.local_port}"
        elif ssh_host and ssh_port:
            grpc_target = f"{ssh_host}:{ssh_port}"
        else:
            grpc_target = ""
        return HostHandle(
            name=host_name,
            sea=self.name,
            instance_id=str(entry.get("id", "")),
            grpc_target=grpc_target,
            state=_STATE_MAP.get(status, status or "unknown"),
            cost_per_hour=_as_float(entry.get("dph_total")),
            created_at_unix_ms=int(self._clock() * 1000),
            ssh_target=ssh_target,
        )

    def list_instances(
        self, state_filter: str | None = None
    ) -> list[HostHandle]:
        handles: dict[str, HostHandle] = {}
        for entry in self._all_instances():
            name = self._host_name_from_label(entry.get("label"))
            if name is None:
                continue  # not a lotsman-managed instance in this sea
            handles[name] = self._handle_from_instance(name, entry)
        # Reconcile internal registry with live truth.
        self._hosts = handles
        result = list(handles.values())
        if state_filter is not None:
            result = [h for h in result if h.state == state_filter]
        return result

    def destroy(self, host_name: str, *, kill_running: bool = False) -> None:
        instance_id = self._require_instance_id(host_name)
        result = self._vast(["destroy", "instance", instance_id])
        if not result.ok:
            raise VastSeaError(
                f"vast destroy failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        self._close_forward(host_name)
        self._hosts.pop(host_name, None)

    def stop(self, host_name: str) -> None:
        instance_id = self._require_instance_id(host_name)
        result = self._vast(["stop", "instance", instance_id])
        if not result.ok:
            raise VastSeaError(
                f"vast stop failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        # A stopped instance loses its ssh endpoint; drop the dead tunnel.
        self._close_forward(host_name)

    def start(self, host_name: str) -> None:
        instance_id = self._require_instance_id(host_name)
        result = self._vast(["start", "instance", instance_id])
        if not result.ok:
            raise VastSeaError(
                f"vast start failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        # On restart the ssh host/port (and thus the tunnel) may change — wait
        # for `running` again and re-establish the forward.
        entry = self._wait_until_ready(instance_id)
        self._open_forward(host_name, entry)
        self._hosts[host_name] = self._handle_from_instance(host_name, entry)

    def _close_forward(self, host_name: str) -> None:
        forward = self._forwards.pop(host_name, None)
        if forward is not None:
            forward.close()

    def close_all_forwards(self) -> None:
        """Tear down every open tunnel (call on Marina shutdown)."""
        for forward in self._forwards.values():
            forward.close()
        self._forwards.clear()

    def renew(self, host_name: str, hours: int) -> None:
        del host_name, hours
        raise NotImplementedError(
            f"sea {self.name!r} (vast): on-demand instances run until destroyed; "
            f"there is no rental to renew"
        )

    # ----- status / cost / balance -----

    def status(self) -> SeaStatus:
        if self._api_key is None:
            return SeaStatus(
                sea=self.name,
                reachable=False,
                detail=f"no API key (set ${self.api_key_env})",
            )
        result = self._vast(["show", "user"])
        if not result.ok:
            return SeaStatus(
                sea=self.name,
                reachable=False,
                detail=result.stderr.strip() or "vastai show user failed",
            )
        balance = self._parse_balance(result.stdout)
        return SeaStatus(
            sea=self.name,
            reachable=True,
            detail="vast.ai reachable",
            balance=balance,
            burn_rate_per_hour=self._running_burn_rate(),
        )

    def balance(self) -> float | None:
        result = self._vast(["show", "user"])
        if not result.ok:
            raise VastSeaError(
                f"vast show user failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return self._parse_balance(result.stdout)

    def _parse_balance(self, stdout: str) -> float | None:
        text = stdout.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return _as_float(data.get("credit"), None)  # type: ignore[arg-type]
        return None

    def _running_burn_rate(self) -> float:
        try:
            return sum(
                h.cost_per_hour
                for h in self.list_instances()
                if h.state == "running"
            )
        except VastSeaError:
            return 0.0

    def cost_summary(self) -> CostBreakdown:
        per_host = tuple(
            (h.name, h.cost_per_hour)
            for h in self.list_instances()
            if h.state == "running"
        )
        total = sum(cost for _, cost in per_host)
        try:
            balance = self.balance()
        except VastSeaError:
            balance = None
        burn_24h = total * 24.0
        days_remaining = (
            balance / burn_24h if balance is not None and burn_24h > 0 else None
        )
        return CostBreakdown(
            sea=self.name,
            total_per_hour=total,
            per_host=per_host,
            balance=balance,
            burn_rate_24h=burn_24h,
            days_remaining_at_balance=days_remaining,
        )

    # ----- helpers -----

    def _require_instance_id(self, host_name: str) -> str:
        handle = self._hosts.get(host_name)
        if handle is None:
            # Try to reconcile from live state before giving up.
            self.list_instances()
            handle = self._hosts.get(host_name)
        if handle is None:
            raise VastSeaError(
                f"unknown host: {host_name!r} in sea {self.name!r}"
            )
        return handle.instance_id

    def _auto_name(self) -> str:
        idx = 1
        while True:
            candidate = f"{self.name}-{idx}"
            if candidate not in self._hosts:
                return candidate
            idx += 1

    # ----- test helpers (not part of Sea Protocol) -----

    def _inject_host(self, handle: HostHandle) -> None:
        """Test-only: pre-seed an internal host without renting anything."""
        self._hosts[handle.name] = handle
