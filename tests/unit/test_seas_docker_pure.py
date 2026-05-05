from __future__ import annotations

import pytest

from marina.seas.base import HostHandle
from marina.seas.docker_sea import DockerSea, DockerSeaCapability

pytestmark = pytest.mark.unit


def _gomer_capability(**overrides: object) -> DockerSeaCapability:
    base: dict[str, object] = dict(
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
    base.update(overrides)
    return DockerSeaCapability(**base)  # type: ignore[arg-type]


def _a100_capability(**overrides: object) -> DockerSeaCapability:
    base: dict[str, object] = dict(
        gpu_model="A100 SXM4",
        gpu_count=1,
        vram_gb=80,
        fp64_native=True,
        cpu_ghz=5.2,
        cpu_cores=8,
        ram_gb=64,
        disk_gb=400,
        price_per_hour=0.0,
    )
    base.update(overrides)
    return DockerSeaCapability(**base)  # type: ignore[arg-type]


# ---- self-offer ----


def test_self_offer_includes_sea_name_and_context() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    [offer] = sea.search()
    assert offer.sea == "gomer"
    assert offer.offer_id == "gomer-local"
    assert offer.gpu_model == "RTX 4070"
    assert offer.fp64_native is False
    assert offer.extras["docker_context"] == "gomer"


def test_search_returns_single_offer_regardless_of_filters() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    assert len(sea.search()) == 1
    assert len(sea.search(filters={"any": "thing"}, limit=5)) == 1


# ---- recommend ----


def test_recommend_dft_paper_grade_rejects_no_fp64() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    assert sea.recommend("dft_paper_grade") == []


def test_recommend_dft_paper_grade_accepts_owned_a100() -> None:
    """Owned A100 hardware passes paper-grade by default (reliability=1.0).

    DockerSeaCapability defaults `reliability=1.0` (owner-attested). Admin
    can override to a lower value in marina.toml if hardware is flaky.
    """
    sea = DockerSea("a100_box", docker_context="a100", capability=_a100_capability())
    [offer] = sea.recommend("dft_paper_grade")
    assert offer.fp64_native is True
    assert offer.reliability == 1.0


def test_recommend_dft_paper_grade_rejects_low_reliability_override() -> None:
    """Admin who marks hardware unreliable falls below the 0.95 threshold."""
    cap = _a100_capability(reliability=0.90)
    sea = DockerSea("flaky", docker_context="flaky", capability=cap)
    assert sea.recommend("dft_paper_grade") == []
    # mlip has no reliability requirement → still passes
    assert len(sea.recommend("mlip")) == 1


def test_recommend_mlip_accepts_rtx4070() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    [offer] = sea.recommend("mlip")
    assert offer.gpu_model == "RTX 4070"


def test_recommend_unknown_workload_raises() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    with pytest.raises(ValueError, match="unknown workload"):
        sea.recommend("does_not_exist")


def test_recommend_filters_by_budget() -> None:
    cap = _gomer_capability(price_per_hour=2.50)
    sea = DockerSea("expensive", docker_context="x", capability=cap)
    assert sea.recommend("mlip", budget_per_hour=1.0) == []
    [offer] = sea.recommend("mlip", budget_per_hour=3.0)
    assert offer.price_per_hour == 2.50


# ---- inventory / cost ----


def test_list_instances_empty_initially() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    assert sea.list_instances() == []


def test_list_instances_filters_by_state() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    sea._inject_host(_make_handle("a", state="running"))
    sea._inject_host(_make_handle("b", state="stopped"))
    sea._inject_host(_make_handle("c", state="running"))

    assert {h.name for h in sea.list_instances()} == {"a", "b", "c"}
    assert {h.name for h in sea.list_instances("running")} == {"a", "c"}
    assert {h.name for h in sea.list_instances("stopped")} == {"b"}


def test_cost_summary_sums_all_hosts() -> None:
    sea = DockerSea(
        "expensive",
        docker_context="x",
        capability=_gomer_capability(price_per_hour=0.50),
    )
    sea._inject_host(_make_handle("a", cost_per_hour=0.50))
    sea._inject_host(_make_handle("b", cost_per_hour=0.50))

    breakdown = sea.cost_summary()
    assert breakdown.sea == "expensive"
    assert breakdown.total_per_hour == 1.0
    assert breakdown.burn_rate_24h == 24.0
    assert breakdown.balance is None
    assert dict(breakdown.per_host) == {"a": 0.50, "b": 0.50}


def test_cost_summary_no_hosts() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    breakdown = sea.cost_summary()
    assert breakdown.total_per_hour == 0.0
    assert breakdown.per_host == ()
    assert breakdown.burn_rate_24h == 0.0


# ---- renew unsupported ----


def test_renew_raises_not_implemented() -> None:
    sea = DockerSea("gomer", docker_context="gomer", capability=_gomer_capability())
    with pytest.raises(NotImplementedError, match="owns its hardware"):
        sea.renew("anything", hours=24)


# ---- helpers ----


def _make_handle(name: str, *, state: str = "running", cost_per_hour: float = 0.0) -> HostHandle:
    return HostHandle(
        name=name,
        sea="testsea",
        instance_id=f"id_{name}",
        grpc_target=f"127.0.0.1:1000{ord(name[0]) % 10}",
        state=state,
        cost_per_hour=cost_per_hour,
        created_at_unix_ms=0,
    )
