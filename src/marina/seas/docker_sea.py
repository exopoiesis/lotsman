from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from marina.seas.base import CostBreakdown, HostHandle, Offer, SeaStatus
from marina.seas.presets import PRESETS, matches
from marina.seas.runner import Runner, RunResult, subprocess_runner


class SeaError(Exception):
    """Raised when an external command (e.g. `docker`) fails."""


@dataclass
class DockerSeaCapability:
    """What a docker context advertises to Marina as its single 'offer'.

    Admin fills these in marina.toml under `[seas.NAME]` — we cannot probe
    a remote docker context for GPU model / VRAM uniformly.

    `reliability` defaults to 1.0 (owner-attested) so owned hardware can
    pass paper-grade workloads without a third-party metric. Override in
    config (`reliability = 0.9`) if your box flakes.
    """

    gpu_model: str
    gpu_count: int
    vram_gb: int
    fp64_native: bool
    cpu_ghz: float
    cpu_cores: int
    ram_gb: int
    disk_gb: int
    price_per_hour: float = 0.0
    reliability: float | None = 1.0
    inet_down_mbps: float | None = None


_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$")
_LOTSMAN_CONTAINER_PORT = 50051
_LOTSMAN_LABEL = "lotsman_managed=1"


class DockerSea:
    """A hosting sea backed by `docker --context <ctx>` on some Linux box.

    Each call to `create()` spawns a fresh container running `lotsman serve`
    on a random host port; Marina then connects to that port via gRPC.
    Owned hardware → no balance, no rental, no renewal.
    """

    def __init__(
        self,
        name: str,
        *,
        docker_context: str,
        capability: DockerSeaCapability,
        runner: Runner | None = None,
        clock: object | None = None,
    ) -> None:
        self.name = name
        self.docker_context = docker_context
        self.capability = capability
        self._runner: Runner = runner if runner is not None else subprocess_runner
        # injectable clock for tests; defaults to time.time
        if clock is None:
            self._clock = time.time
        else:
            self._clock = clock  # type: ignore[assignment]
        self._hosts: dict[str, HostHandle] = {}

    # ----- offers / recommendations (pure logic) -----

    def _self_offer(self) -> Offer:
        cap = self.capability
        return Offer(
            sea=self.name,
            offer_id=f"{self.name}-local",
            gpu_model=cap.gpu_model,
            gpu_count=cap.gpu_count,
            vram_gb=cap.vram_gb,
            fp64_native=cap.fp64_native,
            cpu_ghz=cap.cpu_ghz,
            cpu_cores=cap.cpu_cores,
            ram_gb=cap.ram_gb,
            disk_gb=cap.disk_gb,
            price_per_hour=cap.price_per_hour,
            reliability=cap.reliability,
            inet_down_mbps=cap.inet_down_mbps,
            extras={"docker_context": self.docker_context},
        )

    def search(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 20,
    ) -> list[Offer]:
        del filters, limit  # docker sea has exactly one offer
        return [self._self_offer()]

    def recommend(
        self,
        workload: str,
        budget_per_hour: float | None = None,
        min_hours: int | None = None,
    ) -> list[Offer]:
        del min_hours  # owned hardware: no minimum-rental constraint
        if workload not in PRESETS:
            raise ValueError(
                f"unknown workload {workload!r}; known: {sorted(PRESETS)}"
            )
        offer = self._self_offer()
        if not matches(offer, PRESETS[workload]):
            return []
        if budget_per_hour is not None and offer.price_per_hour > budget_per_hour:
            return []
        return [offer]

    # ----- inventory / cost -----

    def list_instances(
        self, state_filter: str | None = None
    ) -> list[HostHandle]:
        hosts = list(self._hosts.values())
        if state_filter is not None:
            hosts = [h for h in hosts if h.state == state_filter]
        return hosts

    def cost_summary(self) -> CostBreakdown:
        per_host = tuple(
            (h.name, h.cost_per_hour) for h in self._hosts.values()
        )
        total = sum(cost for _, cost in per_host)
        return CostBreakdown(
            sea=self.name,
            total_per_hour=total,
            per_host=per_host,
            balance=None,
            burn_rate_24h=total * 24.0,
            days_remaining_at_balance=None,
        )

    # ----- subprocess-driven lifecycle -----

    def status(self) -> SeaStatus:
        result = self._docker(["info", "--format", "{{.ServerVersion}}"])
        if result.ok:
            return SeaStatus(
                sea=self.name,
                reachable=True,
                detail=f"docker {result.stdout.strip()}",
            )
        return SeaStatus(
            sea=self.name,
            reachable=False,
            detail=result.stderr.strip() or "docker info failed",
        )

    def create(
        self,
        image: str,
        *,
        offer_id: str | None = None,
        name: str | None = None,
        disk_gb: int | None = None,
        onstart: str | None = None,
    ) -> HostHandle:
        del offer_id, disk_gb, onstart  # docker sea ignores these (M2-A scope)
        host_name = name or self._auto_name()
        if not _CONTAINER_NAME_RE.match(host_name):
            raise SeaError(
                f"invalid host name {host_name!r}: must match {_CONTAINER_NAME_RE.pattern}"
            )
        if host_name in self._hosts:
            raise SeaError(f"host {host_name!r} already exists in sea {self.name!r}")

        argv = [
            "docker",
            "--context",
            self.docker_context,
            "run",
            "-d",
            "--name",
            host_name,
            "--label",
            _LOTSMAN_LABEL,
            "--label",
            f"lotsman_sea={self.name}",
            "-p",
            f"0:{_LOTSMAN_CONTAINER_PORT}",
        ]
        if self.capability.gpu_count > 0:
            argv += ["--gpus", "all"]
        argv += [image, "lotsman", "serve"]

        result = self._runner(argv)
        if not result.ok:
            raise SeaError(
                f"docker run failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        instance_id = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""

        host_port = self._lookup_host_port(host_name)
        grpc_target = f"127.0.0.1:{host_port}"

        handle = HostHandle(
            name=host_name,
            sea=self.name,
            instance_id=instance_id,
            grpc_target=grpc_target,
            state="running",
            cost_per_hour=self.capability.price_per_hour,
            created_at_unix_ms=int(self._clock() * 1000),
            ssh_target=None,
        )
        self._hosts[host_name] = handle
        return handle

    def destroy(self, host_name: str, *, kill_running: bool = False) -> None:
        if host_name not in self._hosts:
            raise SeaError(f"unknown host: {host_name!r} in sea {self.name!r}")
        argv = [
            "docker",
            "--context",
            self.docker_context,
            "rm",
        ]
        if kill_running:
            argv.append("-f")
        argv.append(host_name)
        result = self._runner(argv)
        if not result.ok:
            raise SeaError(
                f"docker rm failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        del self._hosts[host_name]

    def stop(self, host_name: str) -> None:
        if host_name not in self._hosts:
            raise SeaError(f"unknown host: {host_name!r} in sea {self.name!r}")
        result = self._docker(["stop", host_name])
        if not result.ok:
            raise SeaError(f"docker stop failed: {result.stderr.strip()}")
        prior = self._hosts[host_name]
        self._hosts[host_name] = HostHandle(
            name=prior.name,
            sea=prior.sea,
            instance_id=prior.instance_id,
            grpc_target=prior.grpc_target,
            state="stopped",
            cost_per_hour=prior.cost_per_hour,
            created_at_unix_ms=prior.created_at_unix_ms,
            ssh_target=prior.ssh_target,
        )

    def start(self, host_name: str) -> None:
        if host_name not in self._hosts:
            raise SeaError(f"unknown host: {host_name!r} in sea {self.name!r}")
        result = self._docker(["start", host_name])
        if not result.ok:
            raise SeaError(f"docker start failed: {result.stderr.strip()}")
        prior = self._hosts[host_name]
        # port may change after restart
        host_port = self._lookup_host_port(host_name)
        self._hosts[host_name] = HostHandle(
            name=prior.name,
            sea=prior.sea,
            instance_id=prior.instance_id,
            grpc_target=f"127.0.0.1:{host_port}",
            state="running",
            cost_per_hour=prior.cost_per_hour,
            created_at_unix_ms=prior.created_at_unix_ms,
            ssh_target=prior.ssh_target,
        )

    def renew(self, host_name: str, hours: int) -> None:
        del host_name, hours
        raise NotImplementedError(
            f"sea {self.name!r} (docker) owns its hardware; no rental to renew"
        )

    # ----- helpers -----

    def _auto_name(self) -> str:
        idx = 1
        while True:
            candidate = f"{self.name}-{idx}"
            if candidate not in self._hosts:
                return candidate
            idx += 1

    def _docker(self, sub_argv: list[str]) -> RunResult:
        return self._runner(["docker", "--context", self.docker_context, *sub_argv])

    def _lookup_host_port(self, container: str) -> int:
        result = self._docker(
            ["inspect", "--format", "{{json .NetworkSettings.Ports}}", container]
        )
        if not result.ok:
            raise SeaError(
                f"docker inspect failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        try:
            ports = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise SeaError(f"docker inspect: bad JSON: {exc}") from exc
        bindings = ports.get(f"{_LOTSMAN_CONTAINER_PORT}/tcp") or []
        if not bindings:
            raise SeaError(
                f"container {container!r} has no host binding for "
                f"{_LOTSMAN_CONTAINER_PORT}/tcp yet"
            )
        host_port_raw = bindings[0].get("HostPort")
        try:
            return int(host_port_raw)
        except (TypeError, ValueError) as exc:
            raise SeaError(f"docker inspect: malformed HostPort {host_port_raw!r}") from exc

    # ----- test helpers (not part of Sea Protocol) -----

    def _inject_host(self, handle: HostHandle) -> None:
        """Test-only: pre-seed an internal host without spawning a container."""
        self._hosts[handle.name] = handle
