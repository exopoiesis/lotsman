"""Service tests for VerdaSea against a scripted HTTP transport.

We inject a FakeTransport (method, path-substring -> HttpResponse) so the OAuth
flow and every endpoint are exercised without touching the network, mirroring
how the Vast tests script the `vastai` CLI runner.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from marina.seas.verda_sea import HttpResponse, VerdaSea, VerdaSeaError

pytestmark = pytest.mark.service


@dataclass
class FakeCall:
    method: str
    url: str
    params: dict | None
    json_body: object


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
        self.calls.append(FakeCall(method.upper(), url, params, json_body))
        for m, contains, resp in self._routes:
            if m == method.upper() and contains in url:
                return resp(self) if callable(resp) else resp
        raise AssertionError(f"FakeTransport: no route for {method} {url}")


def _ok(body: object) -> HttpResponse:
    return HttpResponse(200, json.dumps(body) if not isinstance(body, str) else body)


_TOKEN = _ok({"access_token": "tok-abc", "refresh_token": "r", "expires_in": 3600})

_INSTANCE_TYPES = [
    {
        "instance_type": "1A100.22V",
        "model": "A100 SXM4",
        "cpu": {"number_of_cores": 22},
        "memory": {"size_in_gigabytes": 120},
        "gpu": {"number_of_gpus": 1, "description": "1x NVIDIA A100 SXM4 80GB"},
        "gpu_memory": {"size_in_gigabytes": 80},
        "price_per_hour": 1.79,
        "spot_price": 0.63,
    },
    {
        "instance_type": "2A6000.30V",
        "model": "RTX A6000",
        "cpu": {"number_of_cores": 30},
        "memory": {"size_in_gigabytes": 120},
        "gpu": {"number_of_gpus": 2, "description": "2x NVIDIA RTX A6000 48GB"},
        "gpu_memory": {"size_in_gigabytes": 96},
        "price_per_hour": 1.10,
        "spot_price": 0.40,
    },
]

_AVAIL_OD = [
    {"location_code": "FIN-03", "availabilities": ["1A100.22V", "2A6000.30V"]},
    {"location_code": "ICE-01", "availabilities": ["1A100.22V"]},
]
_AVAIL_SPOT = [
    {"location_code": "FIN-03", "availabilities": ["1A100.22V"]},
]


def _make_sea(transport: FakeTransport, *, clock_start: float = 1000.0) -> VerdaSea:
    t = [clock_start]

    def clock() -> float:
        t[0] += 1.0
        return t[0]

    return VerdaSea(
        "verda",
        client_id="cid",
        client_secret="csecret",
        transport=transport,
        clock=clock,
        sleeper=lambda _s: None,
        ssh_pubkey_path="/fake/key.pub",
        pubkey_loader=lambda _p: "ssh-ed25519 AAAA marina",
        poll_interval_s=0.0,
    )


# ---- search ----


def test_search_on_demand_maps_specs_and_sorts_by_price() -> None:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/instance-types", _ok(_INSTANCE_TYPES))
        .route("GET", "/instance-availability", _ok(_AVAIL_OD))
    )
    sea = _make_sea(transport)
    offers = sea.search()

    # A100 in two regions + A6000 in one = 3 offers, cheapest first (A6000 1.10)
    assert len(offers) == 3
    assert offers[0].gpu_model == "RTX A6000"
    assert offers[0].price_per_hour == 1.10
    a100 = next(o for o in offers if "A100" in o.gpu_model)
    assert a100.fp64_native is True
    assert a100.vram_gb == 80          # 80GB total / 1 GPU
    assert a100.price_per_hour == 1.79  # on-demand price
    assert a100.host_type == "on-demand"
    assert a100.offer_id == "1A100.22V@FIN-03"
    assert a100.zgpu > 0                # datasheet-scored FP64 host
    # consumer card: no native FP64
    a6000 = offers[0]
    assert a6000.fp64_native is False
    assert a6000.vram_gb == 48          # 96GB / 2 GPUs


def test_search_spot_uses_spot_price_and_flag() -> None:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/instance-types", _ok(_INSTANCE_TYPES))
        .route("GET", "/instance-availability", _ok(_AVAIL_SPOT))
    )
    sea = _make_sea(transport)
    offers = sea.search(filters={"host_type": "spot"})

    assert len(offers) == 1
    (spot,) = offers
    assert spot.host_type == "interruptible"
    assert spot.price_per_hour == 0.63
    assert spot.offer_id == "1A100.22V@FIN-03#spot"
    # the availability query carried is_spot=true
    avail_calls = [c for c in transport.calls if "instance-availability" in c.url]
    assert avail_calls and avail_calls[-1].params == {"is_spot": "true"}


def test_search_any_merges_both_pricing_modes() -> None:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/instance-types", _ok(_INSTANCE_TYPES))
        .route("GET", "/instance-availability",
               lambda t: _ok(_AVAIL_SPOT) if t.calls[-1].params == {"is_spot": "true"}
               else _ok(_AVAIL_OD))
    )
    sea = _make_sea(transport)
    offers = sea.search(filters={"host_type": "any"})

    types = {o.host_type for o in offers}
    assert types == {"on-demand", "interruptible"}
    prices = [o.price_per_hour for o in offers]
    assert prices == sorted(prices)


def test_search_gpu_name_and_vram_filter() -> None:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/instance-types", _ok(_INSTANCE_TYPES))
        .route("GET", "/instance-availability", _ok(_AVAIL_OD))
    )
    sea = _make_sea(transport)
    offers = sea.search(filters={"gpu_name": "A100", "vram_gb": 80})
    assert all("A100" in o.gpu_model for o in offers)
    assert {o.geolocation for o in offers} == {"FIN-03", "ICE-01"}


def _avail_sea() -> VerdaSea:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/instance-types", _ok(_INSTANCE_TYPES))
        .route("GET", "/instance-availability", _ok(_AVAIL_OD))
    )
    return _make_sea(transport)


def test_cpu_name_selection_excludes_verda_no_cpu_model() -> None:
    # Selection axis: Verda exposes no CPU model, so a cpu_name request can't be
    # satisfied -> excluded.
    assert _avail_sea().search(filters={"cpu_name": "EPYC"}) == []


@pytest.mark.parametrize("flt", [{"min_cuda": 12.0}, {"min_reliability": 0.9}])
def test_quality_gates_pass_curated_verda(flt: dict) -> None:
    # Quality gates: Verda is a curated datacenter (no per-host CUDA-max or
    # reliability score), so these gates give it the benefit of the doubt and
    # PASS rather than wrongly excluding the whole fleet.
    sea = _avail_sea()
    unfiltered = len(_avail_sea().search())
    gated = sea.search(filters=flt)
    assert gated, "curated Verda should survive a quality-gate filter"
    assert len(gated) == unfiltered  # gate dropped nothing


def test_search_unknown_host_type_raises() -> None:
    transport = FakeTransport().route("POST", "/oauth2/token", _TOKEN)
    sea = _make_sea(transport)
    with pytest.raises(VerdaSeaError, match="unknown host_type"):
        sea.search(filters={"host_type": "bogus"})


# ---- create (host_create) ----


def test_create_provisions_and_returns_ssh_target() -> None:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/ssh-keys",
               _ok([{"id": "k1", "name": "marina-verda", "key": "ssh-ed25519 AAAA marina"}]))
        .route("POST", "/instances", _ok("inst-123"))
        .route("GET", "/instances/inst-123",
               _ok({"id": "inst-123", "status": "running",
                    "hostname": "verda-1A100-22V", "ip": "203.0.113.5"}))
    )
    sea = _make_sea(transport)
    handle = sea.create("ubuntu-24.04-cuda", offer_id="1A100.22V@FIN-03")

    assert handle.instance_id == "inst-123"
    assert handle.state == "running"
    assert handle.ssh_target == "root@203.0.113.5:22"
    # the create payload was well-formed
    create = next(c for c in transport.calls
                  if c.method == "POST" and c.url.endswith("/instances"))
    body = create.json_body
    assert body["instance_type"] == "1A100.22V"
    assert body["location_code"] == "FIN-03"
    assert body["is_spot"] is False
    assert body["contract"] == "PAY_AS_YOU_GO"
    assert body["ssh_key_ids"] == ["k1"]


def test_create_spot_offer_sets_spot_contract() -> None:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/ssh-keys",
               _ok([{"id": "k1", "name": "x", "key": "ssh-ed25519 AAAA marina"}]))
        .route("POST", "/instances", _ok("inst-9"))
        .route("GET", "/instances/inst-9",
               _ok({"id": "inst-9", "status": "running", "hostname": "h",
                    "ip": "198.51.100.7"}))
    )
    sea = _make_sea(transport)
    sea.create("img", offer_id="1A100.22V@FIN-03#spot")
    create = next(c for c in transport.calls
                  if c.method == "POST" and c.url.endswith("/instances"))
    assert create.json_body["is_spot"] is True
    assert create.json_body["contract"] == "SPOT"


def test_create_registers_ssh_key_when_absent() -> None:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/ssh-keys", _ok([]))      # none registered yet
        .route("POST", "/ssh-keys", HttpResponse(200, "new-key-id"))
        .route("POST", "/instances", _ok("inst-1"))
        .route("GET", "/instances/inst-1",
               _ok({"id": "inst-1", "status": "running", "hostname": "h",
                    "ip": "192.0.2.1"}))
    )
    sea = _make_sea(transport)
    sea.create("img", offer_id="1A100.22V@FIN-03")
    create = next(c for c in transport.calls
                  if c.method == "POST" and c.url.endswith("/instances"))
    assert create.json_body["ssh_key_ids"] == ["new-key-id"]


def test_create_requires_offer_id() -> None:
    sea = _make_sea(FakeTransport())
    with pytest.raises(VerdaSeaError, match="requires offer_id"):
        sea.create("img", offer_id=None)


def test_parse_offer_id_round_trip() -> None:
    assert VerdaSea._parse_offer_id("1A100.22V@FIN-03") == ("1A100.22V", "FIN-03", False)
    assert VerdaSea._parse_offer_id("1A100.22V@FIN-03#spot") == (
        "1A100.22V", "FIN-03", True)
    with pytest.raises(VerdaSeaError):
        VerdaSea._parse_offer_id("no-region")


# ---- status / auth ----


def test_status_reachable_after_auth() -> None:
    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/instances", _ok([]))
    )
    sea = _make_sea(transport)
    s = sea.status()
    assert s.reachable is True


def test_status_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # No creds passed and the env vars unset -> cleanly unreachable (no file
    # fallback, so this stays deterministic regardless of the host's ~/.verda).
    monkeypatch.delenv("VERDA_CLIENT_ID", raising=False)
    monkeypatch.delenv("VERDA_CLIENT_SECRET", raising=False)
    sea = VerdaSea("verda", client_id=None, client_secret=None,
                   transport=FakeTransport())
    s = sea.status()
    assert s.reachable is False
    assert "credentials" in s.detail


def test_credentials_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDA_CLIENT_ID", "env-id")
    monkeypatch.setenv("VERDA_CLIENT_SECRET", "env-secret")
    sea = VerdaSea("verda", transport=FakeTransport())
    assert sea._client_id == "env-id"
    assert sea._client_secret == "env-secret"


def test_request_reauths_once_on_401() -> None:
    state = {"n": 0}

    def instances(_t: FakeTransport) -> HttpResponse:
        state["n"] += 1
        return HttpResponse(401, "{}") if state["n"] == 1 else _ok([])

    transport = (
        FakeTransport()
        .route("POST", "/oauth2/token", _TOKEN)
        .route("GET", "/instances", instances)
    )
    sea = _make_sea(transport)
    # first call gets 401 -> re-auth -> retry succeeds
    assert sea.list_instances() == []
    token_calls = [c for c in transport.calls if "/oauth2/token" in c.url]
    assert len(token_calls) == 2
