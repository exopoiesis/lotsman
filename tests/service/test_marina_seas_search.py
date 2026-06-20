"""Hub.seas_search — the cross-sea uber search that merges all marketplaces."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from marina.hub import Hub
from marina.seas.base import Offer

pytestmark = pytest.mark.service


def _offer(sea: str, oid: str, price: float, vram: int = 80) -> Offer:
    return Offer(
        sea=sea, offer_id=oid, gpu_model="A100", gpu_count=1, vram_gb=vram,
        fp64_native=True, cpu_ghz=3.0, cpu_cores=16, ram_gb=64, disk_gb=200,
        price_per_hour=price,
    )


@dataclass
class FakeMarketSea:
    name: str
    offers: list[Offer]
    is_marketplace: bool = True
    seen_filters: dict | None = None
    seen_limit: int = -1

    def search(self, filters=None, limit=20):
        self.seen_filters = dict(filters or {})
        self.seen_limit = limit
        return self.offers[:limit]

    # minimal Sea protocol stubs (unused here)
    def recommend(self, *a, **k): ...
    def status(self): ...
    def cost_summary(self): ...
    def list_instances(self, state_filter=None): return []
    def create(self, *a, **k): ...
    def destroy(self, *a, **k): ...
    def stop(self, n): ...
    def start(self, n): ...
    def renew(self, n, h): ...


@dataclass
class FakeDockerSea(FakeMarketSea):
    is_marketplace: bool = False  # owned hardware — must be excluded


@dataclass
class FakeErrorSea:
    name: str
    is_marketplace: bool = True

    def search(self, filters=None, limit=20):
        raise RuntimeError("no credentials")

    def recommend(self, *a, **k): ...
    def status(self): ...
    def cost_summary(self): ...
    def list_instances(self, state_filter=None): return []
    def create(self, *a, **k): ...
    def destroy(self, *a, **k): ...
    def stop(self, n): ...
    def start(self, n): ...
    def renew(self, n, h): ...


def test_seas_search_merges_marketplaces_skips_docker() -> None:
    vast = FakeMarketSea("vast", [_offer("vast", "v1", 1.0), _offer("vast", "v2", 2.0)])
    verda = FakeMarketSea("verda", [_offer("verda", "d1", 1.5)])
    gomer = FakeDockerSea("gomer", [_offer("gomer", "g1", 0.0)])  # excluded
    hub = Hub(seas=[vast, verda, gomer])

    merged, errors = hub.seas_search(filters={"gpu_name": "A100"}, limit_per_sea=7)

    seas = {o.sea for o in merged}
    assert seas == {"vast", "verda"}          # gomer (non-marketplace) excluded
    assert len(merged) == 3
    assert errors == {}
    # filter + per-sea limit propagated to each marketplace
    assert vast.seen_filters == {"gpu_name": "A100"}
    assert vast.seen_limit == 7


def test_seas_search_strips_order_from_per_sea_filters() -> None:
    # `order` is applied globally by the caller; forwarding it per-sea would
    # drop any sea lacking that sort key (e.g. Verda has no zcpu).
    vast = FakeMarketSea("vast", [_offer("vast", "v1", 1.0)])
    hub = Hub(seas=[vast])
    merged, errors = hub.seas_search(
        filters={"gpu_name": "A100", "order": "-zcpu"}, limit_per_sea=7
    )
    assert vast.seen_filters == {"gpu_name": "A100"}  # no 'order' forwarded
    assert errors == {} and len(merged) == 1


def test_seas_search_respects_limit_per_sea() -> None:
    many = [_offer("vast", f"v{i}", float(i)) for i in range(10)]
    hub = Hub(seas=[FakeMarketSea("vast", many)])
    merged, _ = hub.seas_search(limit_per_sea=3)
    assert len(merged) == 3


def test_seas_search_collects_per_sea_errors_without_sinking_others() -> None:
    vast = FakeMarketSea("vast", [_offer("vast", "v1", 1.0)])
    clore = FakeErrorSea("clore")
    hub = Hub(seas=[vast, clore])

    merged, errors = hub.seas_search()

    assert [o.sea for o in merged] == ["vast"]   # vast still returned
    assert "clore" in errors
    assert "no credentials" in errors["clore"]
