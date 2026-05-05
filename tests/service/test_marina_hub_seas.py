"""Hub ↔ Sea integration: host_create / host_destroy / sea queries.

These use a FakeSea — an in-memory stand-in for DockerSea that records calls
and hands back synthesised HostHandles whose `grpc_target` points at a real
lotsman_tcp fixture (so the gRPC channel will open). This lets us test the
Hub-side wiring without spawning real docker containers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from marina.hub import HostError, Hub, SeaNotFoundError
from marina.seas.base import CostBreakdown, HostHandle, Offer, SeaStatus

pytestmark = pytest.mark.service


@dataclass
class FakeSea:
    name: str
    grpc_target: str = "127.0.0.1:1"  # overwritten per-test to point at lotsman_tcp
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    destroy_calls: list[tuple[str, bool]] = field(default_factory=list)
    _hosts: dict[str, HostHandle] = field(default_factory=dict)
    _next_id: int = 1

    # ----- Sea protocol -----

    def search(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 20,
    ) -> list[Offer]:
        return [self._offer()]

    def recommend(
        self,
        workload: str,
        budget_per_hour: float | None = None,
        min_hours: int | None = None,
    ) -> list[Offer]:
        return [self._offer()]

    def status(self) -> SeaStatus:
        return SeaStatus(sea=self.name, reachable=True, detail="fake")

    def cost_summary(self) -> CostBreakdown:
        per_host = tuple(
            (h.name, h.cost_per_hour) for h in self._hosts.values()
        )
        total = sum(c for _, c in per_host)
        return CostBreakdown(
            sea=self.name,
            total_per_hour=total,
            per_host=per_host,
            burn_rate_24h=total * 24.0,
        )

    def list_instances(self, state_filter: str | None = None) -> list[HostHandle]:
        hs = list(self._hosts.values())
        if state_filter is not None:
            hs = [h for h in hs if h.state == state_filter]
        return hs

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
        host_name = name or f"{self.name}-{self._next_id}"
        self._next_id += 1
        self.create_calls.append(
            dict(
                image=image,
                offer_id=offer_id,
                name=host_name,
                disk_gb=disk_gb,
                onstart=onstart,
                env=env,
            )
        )
        handle = HostHandle(
            name=host_name,
            sea=self.name,
            instance_id=f"id_{host_name}",
            grpc_target=self.grpc_target,
            state="running",
            cost_per_hour=0.0,
            created_at_unix_ms=0,
        )
        self._hosts[host_name] = handle
        return handle

    def destroy(self, host_name: str, *, kill_running: bool = False) -> None:
        self.destroy_calls.append((host_name, kill_running))
        if host_name not in self._hosts:
            raise KeyError(host_name)
        del self._hosts[host_name]

    def stop(self, host_name: str) -> None:
        prior = self._hosts[host_name]
        self._hosts[host_name] = HostHandle(
            **{**prior.__dict__, "state": "stopped"}
        )

    def start(self, host_name: str) -> None:
        prior = self._hosts[host_name]
        self._hosts[host_name] = HostHandle(
            **{**prior.__dict__, "state": "running"}
        )

    def renew(self, host_name: str, hours: int) -> None:
        raise NotImplementedError

    # ----- helpers -----

    def _offer(self) -> Offer:
        return Offer(
            sea=self.name,
            offer_id=f"{self.name}-fake",
            gpu_model="A100",
            gpu_count=1,
            vram_gb=80,
            fp64_native=True,
            cpu_ghz=5.0,
            cpu_cores=8,
            ram_gb=64,
            disk_gb=400,
            price_per_hour=0.85,
            reliability=0.97,
        )


# ---- sea registration ----


def test_sea_register_and_list() -> None:
    hub = Hub()
    fake = FakeSea("vast")
    hub.sea_register(fake)
    assert hub.sea_list() == ["vast"]
    assert hub.sea_get("vast") is fake


def test_sea_register_duplicate_raises() -> None:
    hub = Hub()
    hub.sea_register(FakeSea("vast"))
    with pytest.raises(SeaNotFoundError, match="already registered"):
        hub.sea_register(FakeSea("vast"))


def test_sea_get_unknown_raises() -> None:
    hub = Hub()
    with pytest.raises(SeaNotFoundError, match="unknown sea"):
        hub.sea_get("nonexistent")


def test_seed_seas_via_constructor() -> None:
    a = FakeSea("a")
    b = FakeSea("b")
    hub = Hub(seas=[a, b])
    assert hub.sea_list() == ["a", "b"]


# ---- host_create / host_destroy ----


def test_host_create_dispatches_to_sea_and_registers(lotsman_tcp) -> None:
    fake = FakeSea("gomer", grpc_target=lotsman_tcp.target)
    hub = Hub(seas=[fake])
    try:
        handle = hub.host_create("gomer", image="lotsman:latest", name="gomer-1")
        assert handle.name == "gomer-1"
        assert handle.sea == "gomer"
        assert hub.host_list() == ["gomer-1"]
        # ensure the host was tagged with its owning sea
        assert hub.hosts["gomer-1"].sea == "gomer"
        # sea recorded the call
        assert len(fake.create_calls) == 1
        assert fake.create_calls[0]["image"] == "lotsman:latest"
    finally:
        hub.shutdown()


def test_host_create_unknown_sea_raises() -> None:
    hub = Hub()
    with pytest.raises(SeaNotFoundError):
        hub.host_create("noexist", image="img")


def test_host_destroy_dispatches_to_owning_sea(lotsman_tcp) -> None:
    fake = FakeSea("gomer", grpc_target=lotsman_tcp.target)
    hub = Hub(seas=[fake])
    try:
        hub.host_create("gomer", image="img", name="gomer-1")
        hub.host_destroy("gomer-1")
        assert hub.host_list() == []
        assert fake.destroy_calls == [("gomer-1", False)]
    finally:
        hub.shutdown()


def test_host_destroy_with_kill_running(lotsman_tcp) -> None:
    fake = FakeSea("gomer", grpc_target=lotsman_tcp.target)
    hub = Hub(seas=[fake])
    try:
        hub.host_create("gomer", image="img", name="gomer-1")
        hub.host_destroy("gomer-1", kill_running=True)
        assert fake.destroy_calls == [("gomer-1", True)]
    finally:
        hub.shutdown()


def test_host_destroy_unregisters_manual_host(lotsman_tcp) -> None:
    """host_add'ed hosts (no sea) just close + forget — Marina doesn't own the box."""
    hub = Hub()
    try:
        hub.host_add("legacy", lotsman_tcp.target)
        hub.host_destroy("legacy")
        assert hub.host_list() == []
    finally:
        hub.shutdown()


def test_host_destroy_unknown_raises() -> None:
    hub = Hub()
    with pytest.raises(HostError, match="unknown host"):
        hub.host_destroy("ghost")


def test_host_list_filter_by_sea(lotsman_tcp) -> None:
    fake = FakeSea("gomer", grpc_target=lotsman_tcp.target)
    hub = Hub(seas=[fake])
    try:
        hub.host_create("gomer", image="img", name="g1")
        hub.host_add("legacy", lotsman_tcp.target)  # manual, no sea

        assert hub.host_list() == ["g1", "legacy"]
        assert hub.host_list(sea="gomer") == ["g1"]
        assert hub.host_list(sea="vast") == []
    finally:
        hub.shutdown()


# ---- sea_search / sea_recommend / cost_summary ----


def test_sea_search_returns_offers() -> None:
    hub = Hub(seas=[FakeSea("vast")])
    offers = hub.sea_search("vast")
    assert len(offers) == 1
    assert offers[0].sea == "vast"


def test_sea_recommend_returns_offers() -> None:
    hub = Hub(seas=[FakeSea("vast")])
    offers = hub.sea_recommend("vast", workload="dft_paper_grade")
    assert len(offers) == 1


def test_sea_status_returns_status() -> None:
    hub = Hub(seas=[FakeSea("vast")])
    s = hub.sea_status("vast")
    assert s.reachable is True


def test_cost_summary_per_sea(lotsman_tcp) -> None:
    fake = FakeSea("vast", grpc_target=lotsman_tcp.target)
    hub = Hub(seas=[fake])
    try:
        hub.host_create("vast", image="img", name="h1")
        # FakeSea default cost_per_hour is 0; check shape
        breakdown = hub.cost_summary(sea="vast")
        assert breakdown.sea == "vast"
        assert ("h1", 0.0) in breakdown.per_host
    finally:
        hub.shutdown()


def test_cost_summary_aggregated_no_sea_arg(lotsman_tcp) -> None:
    a = FakeSea("a", grpc_target=lotsman_tcp.target)
    b = FakeSea("b", grpc_target=lotsman_tcp.target)
    hub = Hub(seas=[a, b])
    try:
        hub.host_create("a", image="img", name="ha")
        hub.host_create("b", image="img", name="hb")
        breakdown = hub.cost_summary()
        assert breakdown.sea is None
        names = [n for n, _ in breakdown.per_host]
        assert set(names) == {"ha", "hb"}
    finally:
        hub.shutdown()
