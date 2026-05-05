from __future__ import annotations

import pytest

from marina.seas.base import CostBreakdown, HostHandle, Offer, SeaStatus

pytestmark = pytest.mark.unit


def _gomer_offer_kwargs() -> dict[str, object]:
    return dict(
        sea="gomer",
        offer_id="gomer-local",
        gpu_model="RTX 4070",
        gpu_count=1,
        vram_gb=12,
        fp64_native=False,
        cpu_ghz=5.7,
        cpu_cores=8,
        ram_gb=32,
        disk_gb=500,
        price_per_hour=0.0,
    )


def test_offer_required_fields_construct() -> None:
    offer = Offer(**_gomer_offer_kwargs())  # type: ignore[arg-type]
    assert offer.sea == "gomer"
    assert offer.reliability is None
    assert offer.extras == {}


def test_offer_is_frozen() -> None:
    offer = Offer(**_gomer_offer_kwargs())  # type: ignore[arg-type]
    with pytest.raises((AttributeError, TypeError)):
        offer.sea = "vast"  # type: ignore[misc]


def test_host_handle_default_ssh_none() -> None:
    h = HostHandle(
        name="gomer-1",
        sea="gomer",
        instance_id="abc123",
        grpc_target="localhost:50051",
        state="running",
        cost_per_hour=0.0,
        created_at_unix_ms=0,
    )
    assert h.ssh_target is None
    assert h.state == "running"


def test_cost_breakdown_defaults() -> None:
    c = CostBreakdown(sea="gomer", total_per_hour=0.0, per_host=())
    assert c.burn_rate_24h == 0.0
    assert c.balance is None
    assert c.days_remaining_at_balance is None


def test_sea_status_minimal() -> None:
    s = SeaStatus(sea="gomer", reachable=True)
    assert s.detail == ""
    assert s.balance is None
