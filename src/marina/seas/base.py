from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Offer:
    """A single hosting offer in some sea (provider).

    Vast.ai: one searched contract.  Docker sea: the local docker context as a
    single static "offer" derived from manifest.toml capability fields.
    """

    sea: str
    offer_id: str
    gpu_model: str
    gpu_count: int
    vram_gb: int
    fp64_native: bool
    cpu_ghz: float
    cpu_cores: int
    ram_gb: int
    disk_gb: int
    price_per_hour: float
    reliability: float | None = None
    inet_down_mbps: float | None = None
    extras: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HostHandle:
    """A provisioned host: gomer container, vast instance, etc."""

    name: str
    sea: str
    instance_id: str
    grpc_target: str
    state: str
    cost_per_hour: float
    created_at_unix_ms: int
    ssh_target: str | None = None


@dataclass(frozen=True)
class CostBreakdown:
    sea: str | None
    total_per_hour: float
    per_host: tuple[tuple[str, float], ...]
    balance: float | None = None
    burn_rate_24h: float = 0.0
    days_remaining_at_balance: float | None = None


@dataclass(frozen=True)
class SeaStatus:
    sea: str
    reachable: bool
    detail: str = ""
    balance: float | None = None
    burn_rate_per_hour: float | None = None


@runtime_checkable
class Sea(Protocol):
    """A hosting provider (gomer, vast, runpod, ...).

    Each Sea instance owns a registered name (like 'gomer' or 'vast_main') and
    encapsulates how to enumerate offers, provision hosts, and tear them down.
    Marina holds a registry of Sea instances and dispatches host_create / etc.
    by the `sea=` argument of the MCP call.
    """

    name: str

    def search(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 20,
    ) -> list[Offer]: ...

    def recommend(
        self,
        workload: str,
        budget_per_hour: float | None = None,
        min_hours: int | None = None,
    ) -> list[Offer]: ...

    def status(self) -> SeaStatus: ...

    def cost_summary(self) -> CostBreakdown: ...

    def list_instances(
        self, state_filter: str | None = None
    ) -> list[HostHandle]: ...

    def create(
        self,
        image: str,
        *,
        offer_id: str | None = None,
        name: str | None = None,
        disk_gb: int | None = None,
        onstart: str | None = None,
        env: dict[str, str] | None = None,
    ) -> HostHandle: ...

    def destroy(self, host_name: str, *, kill_running: bool = False) -> None: ...

    def stop(self, host_name: str) -> None: ...

    def start(self, host_name: str) -> None: ...

    def renew(self, host_name: str, hours: int) -> None: ...
