"""Service tests for CloreSea against a scripted HTTP transport.

Fixtures mirror the live Clore.ai `/marketplace` schema (verified against the
real API) so the mapping, filtering, and create-body are exercised offline.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from marina.seas.clore_sea import CloreSea, CloreSeaError
from marina.seas.http_transport import HttpResponse

pytestmark = pytest.mark.service


@dataclass
class FakeCall:
    method: str
    url: str
    params: dict | None
    json_body: object
    headers: dict | None


class FakeTransport:
    """Scripts responses by (method, path-substring), in registration order."""

    def __init__(self) -> None:
        self.calls: list[FakeCall] = []
        self._routes: list[tuple[str, str, HttpResponse | Callable]] = []

    def route(self, method: str, contains: str, resp) -> FakeTransport:
        self._routes.append((method.upper(), contains, resp))
        return self

    def __call__(self, method, url, *, headers=None, params=None,
                 json_body=None, timeout=45.0):
        self.calls.append(FakeCall(method.upper(), url, params, json_body, headers))
        for m, contains, resp in self._routes:
            if m == method.upper() and contains in url:
                return resp(self) if callable(resp) else resp
        raise AssertionError(f"FakeTransport: no route for {method} {url}")


def _ok(body: object) -> HttpResponse:
    return HttpResponse(200, json.dumps(body) if not isinstance(body, str) else body)


# Two rentable servers (one A100 with a spot price, one consumer 3080 without)
# plus one already-rented server that search must drop. Shape per the live API.
_MARKETPLACE = {
    "code": 0,
    "my_servers": [],
    "servers": [
        {
            "id": 200001,
            "rented": False,
            "reliability": 0.992,
            "cuda_version": "12.4",
            "gpu_array": ["A100 SXM4"],
            "allowed_coins": ["bitcoin", "CLORE-Blockchain"],
            "specs": {
                "cpu": "AMD EPYC 7402 24-Core Processor @ 2.80GHz",
                "cpus": "24/48",
                "ram": 128.0,
                "disk": "NVMe 512.0GB",
                "gpu": "1x NVIDIA A100 SXM4 80GB",
                "gpuram": 80,
                "net": {"cc": "DE", "down": 950.0, "up": 800.0},
            },
            "price": {"usd": {"on_demand_usd": 1.40, "spot": 0.70}},
        },
        {
            "id": 200002,
            "rented": False,
            "reliability": 0.45,
            "cuda_version": "13.0",
            "gpu_array": ["RTX 3080"],
            "allowed_coins": ["bitcoin"],
            "specs": {
                "cpu": "Intel(R) Core(TM) i3-3220 CPU @ 3.30GHz",
                "cpus": "2/4",
                "ram": 15.6,
                "disk": "SSD 47.34GB",
                "gpu": "1x NVIDIA GeForce RTX 3080",
                "gpuram": 12,
                "net": {"cc": "RU", "down": 5.0, "up": 0.01},
            },
            "price": {"usd": {"on_demand_usd": 0.20, "spot": 0}},  # no spot
        },
        {
            "id": 200003,
            "rented": True,  # must be filtered out
            "gpu_array": ["A100 SXM4"],
            "specs": {"gpu": "1x NVIDIA A100 SXM4 80GB", "gpuram": 80,
                      "cpu": "x @ 3.0GHz", "cpus": "8/16", "ram": 64.0,
                      "disk": "1000GB", "net": {"cc": "US", "down": 100}},
            "price": {"usd": {"on_demand_usd": 1.0, "spot": 0.5}},
        },
    ],
}


def _make_sea(transport: FakeTransport, *, clock_start: float = 1000.0) -> CloreSea:
    t = [clock_start]

    def clock() -> float:
        t[0] += 1.0
        return t[0]

    return CloreSea(
        "clore",
        api_key="tok-xyz",
        transport=transport,
        clock=clock,
        sleeper=lambda _s: None,
        ssh_pubkey_path="/fake/key.pub",
        pubkey_loader=lambda _p: "ssh-ed25519 AAAA marina",
        poll_interval_s=0.0,
    )


# ---- search ----


def test_search_on_demand_maps_specs_filters_rented_sorts_by_price() -> None:
    transport = FakeTransport().route("GET", "/marketplace", _ok(_MARKETPLACE))
    sea = _make_sea(transport)
    offers = sea.search()

    # rented server dropped -> 2 offers; cheapest (3080 @0.20) first
    assert len(offers) == 2
    assert offers[0].gpu_model == "RTX 3080"
    assert offers[0].price_per_hour == 0.20
    assert offers[0].fp64_native is False

    a100 = next(o for o in offers if "A100" in o.gpu_model)
    assert a100.offer_id == "200001"
    assert a100.host_type == "on-demand"
    assert a100.price_per_hour == 1.40
    assert a100.vram_gb == 80
    assert a100.fp64_native is True
    assert a100.cpu_ghz == 2.80          # parsed from the cpu string
    assert a100.cpu_cores == 24          # "24/48" -> cores/threads
    assert a100.cpu_cores_total == 48
    assert a100.geolocation == "DE"
    assert a100.cuda_max_good == 12.4
    assert a100.zgpu > 0                 # datasheet FP64-scored
    assert a100.zcpu > 0                 # real (clock + family available)
    # auth header carried the raw token + a browser UA (Cloudflare)
    hdr = transport.calls[0].headers
    assert hdr["auth"] == "tok-xyz"
    assert "Mozilla" in hdr["User-Agent"]


def test_search_spot_only_servers_with_spot_price() -> None:
    transport = FakeTransport().route("GET", "/marketplace", _ok(_MARKETPLACE))
    sea = _make_sea(transport)
    offers = sea.search(filters={"host_type": "spot"})

    # only the A100 has spot>0; the 3080 (spot=0) is excluded
    assert len(offers) == 1
    (spot,) = offers
    assert spot.gpu_model == "A100 SXM4"
    assert spot.host_type == "interruptible"
    assert spot.price_per_hour == 0.70
    assert spot.offer_id == "200001#spot"


def test_search_any_merges_both_modes_one_marketplace_call() -> None:
    transport = FakeTransport().route("GET", "/marketplace", _ok(_MARKETPLACE))
    sea = _make_sea(transport)
    offers = sea.search(filters={"host_type": "any"})

    types = {o.host_type for o in offers}
    assert types == {"on-demand", "interruptible"}
    # Clore returns both prices in ONE response — no second call
    assert sum(1 for c in transport.calls if "/marketplace" in c.url) == 1
    prices = [o.price_per_hour for o in offers]
    assert prices == sorted(prices)


def test_search_gpu_name_and_reliability_filter() -> None:
    transport = FakeTransport().route("GET", "/marketplace", _ok(_MARKETPLACE))
    sea = _make_sea(transport)
    offers = sea.search(filters={"gpu_name": "A100", "min_reliability": 0.9})
    assert len(offers) == 1
    assert offers[0].gpu_model == "A100 SXM4"


def test_search_cpu_name_filter() -> None:
    transport = FakeTransport().route("GET", "/marketplace", _ok(_MARKETPLACE))
    sea = _make_sea(transport)
    # only the A100 host has an EPYC; the 3080 host is an i3
    epyc = sea.search(filters={"cpu_name": "EPYC"})
    assert len(epyc) == 1 and epyc[0].gpu_model == "A100 SXM4"
    i3 = sea.search(filters={"cpu_name": "i3-3220"})
    assert len(i3) == 1 and i3[0].gpu_model == "RTX 3080"


def test_search_min_cuda_filter() -> None:
    transport = FakeTransport().route("GET", "/marketplace", _ok(_MARKETPLACE))
    sea = _make_sea(transport)
    # A100 host is cuda 12.4, 3080 host is 13.0 -> min_cuda 12.5 keeps only 3080
    kept = sea.search(filters={"min_cuda": 12.5})
    assert len(kept) == 1 and kept[0].gpu_model == "RTX 3080"


def test_search_unknown_host_type_raises() -> None:
    sea = _make_sea(FakeTransport())
    with pytest.raises(CloreSeaError, match="unknown host_type"):
        sea.search(filters={"host_type": "bogus"})


# ---- create (host_create) ----


def test_create_on_demand_builds_body_and_returns_ssh_target() -> None:
    order = {"id": 77, "renting_server": 200001, "pub_ip": "203.0.113.9",
             "ssh_port": 10022, "price": 1.40}
    transport = (
        FakeTransport()
        .route("POST", "/create_order", _ok({"code": 0}))
        .route("GET", "/my_orders", _ok({"orders": [order], "code": 0}))
    )
    sea = _make_sea(transport)
    handle = sea.create("cloreai/ubuntu22.04", offer_id="200001")

    assert handle.instance_id == "77"
    assert handle.state == "running"
    assert handle.ssh_target == "root@203.0.113.9:10022"
    create = next(c for c in transport.calls if c.url.endswith("/create_order"))
    body = create.json_body
    assert body["renting_server"] == 200001
    assert body["type"] == "on-demand"
    assert body["currency"] == "bitcoin"
    assert body["image"] == "cloreai/ubuntu22.04"
    assert body["ssh_key"] == "ssh-ed25519 AAAA marina"
    assert body["autossh_entrypoint"] is True
    assert "spotprice" not in body


def test_create_spot_sets_type_and_spotprice_from_marketplace() -> None:
    order = {"id": 88, "renting_server": 200001, "pub_ip": "198.51.100.4"}
    transport = (
        FakeTransport()
        .route("POST", "/create_order", _ok({"code": 0}))
        .route("GET", "/marketplace", _ok(_MARKETPLACE))
        .route("GET", "/my_orders", _ok({"orders": [order], "code": 0}))
    )
    sea = _make_sea(transport)
    sea.create("img", offer_id="200001#spot")
    create = next(c for c in transport.calls if c.url.endswith("/create_order"))
    assert create.json_body["type"] == "spot"
    assert create.json_body["spotprice"] == 0.70  # current marketplace spot price


def test_create_requires_offer_id() -> None:
    sea = _make_sea(FakeTransport())
    with pytest.raises(CloreSeaError, match="requires offer_id"):
        sea.create("img", offer_id=None)


def test_parse_offer_id() -> None:
    assert CloreSea._parse_offer_id("200001") == (200001, False)
    assert CloreSea._parse_offer_id("200001#spot") == (200001, True)
    with pytest.raises(CloreSeaError):
        CloreSea._parse_offer_id("not-an-int")


# ---- status / auth ----


def test_status_reachable() -> None:
    transport = FakeTransport().route("GET", "/my_orders", _ok({"orders": []}))
    assert _make_sea(transport).status().reachable is True


def test_status_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLORE_API_KEY", raising=False)
    sea = CloreSea("clore", api_key=None, transport=FakeTransport())
    s = sea.status()
    assert s.reachable is False
    assert "API key" in s.detail


def test_cloudflare_block_surfaces_clear_hint() -> None:
    transport = FakeTransport().route(
        "GET", "/my_orders", HttpResponse(403, "error code: 1010")
    )
    s = _make_sea(transport).status()
    assert s.reachable is False
    assert "Cloudflare" in s.detail


def test_key_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLORE_API_KEY", "env-tok")
    sea = CloreSea("clore", transport=FakeTransport())
    assert sea._api_key == "env-tok"
