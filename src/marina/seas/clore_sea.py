"""CloreSea — a hosting sea backed by the Clore.ai REST API.

A third marketplace alongside Vast and Verda; crypto-settled (Bitcoin / CLORE /
USD-stable), so a useful payment-diverse backup. Like VerdaSea it speaks REST
over an injectable transport (default stdlib `urllib`, no new dependency); like
VastSea the secret is a single API token, read from the process env (a
gitignored `.env`), never the TOML.

Two Clore specifics learned from the live API:
  - **Cloudflare fronts the API** and blocks default user-agents (HTTP 403,
    body ``error code: 1010``). Every call must send a browser-like User-Agent.
  - **Auth header is literally ``auth: <token>``** (the raw token, no "Bearer").
  - Aggressive rate limit: 1 req/s general, create_order 1 per 5 s.

API shape (verified against docs.clore.ai + the live endpoint):
  - ``GET /marketplace``  -> ``{servers:[...], my_servers:[...], code}``. No
    server-side filtering — the whole list comes back; we filter python-side.
    Each server: ``id``, ``rented``, ``reliability``, ``cuda_version``,
    ``gpu_array``, ``specs`` (cpu/cpus/ram/disk/gpu/gpuram/net.cc/...), and
    ``price.usd`` (``on_demand_usd`` and ``spot`` in USD/hr; ``spot`` > 0 means
    a spot order is offered).
  - ``POST /create_order`` ``{currency, image, renting_server, type,
    spotprice?, ssh_key?, autossh_entrypoint?, command?, env?, required_price?}``.
  - ``POST /cancel_order`` ``{id, issue?}``;  ``GET /my_orders``.

Scope: the two commands worth evaluating Clore on — ``search`` (sea_search) and
``create`` (host_create) — plus the minimum of the Sea protocol around them
(status / list / destroy). ``create`` returns a usable ``root@<ip>`` ssh_target
once the order is provisioned.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable

from marina.seas.base import CostBreakdown, HostHandle, Offer, SeaStatus
from marina.seas.http_transport import (
    HttpResponse,
    Transport,
    TransportError,
    urllib_transport,
)
from marina.seas.offer_filters import apply_common_filters
from marina.seas.perf_score import gpu_mem_bandwidth, zcpu, zgpu
from marina.seas.vast_sea import fp64_native

__all__ = ["CloreSea", "CloreSeaError"]

_DEFAULT_BASE_URL = "https://api.clore.ai/v1"
_DEFAULT_TIMEOUT_S = 45.0
_DEFAULT_IMAGE = "cloreai/ubuntu22.04-cuda-12.4"
_DEFAULT_CURRENCY = "bitcoin"  # also "CLORE-Blockchain" (token discount), "USD-Blockchain"
_DEFAULT_READY_TIMEOUT_S = 1800.0
_DEFAULT_POLL_INTERVAL_S = 15.0  # Clore rate-limits hard (1 req/s); poll gently
# Cloudflare blocks non-browser agents with HTTP 403 "error code: 1010".
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Offer sort keys -> Offer attribute (subset meaningful for Clore data).
_ORDER_KEYS = {
    "price": "price_per_hour", "dph": "price_per_hour",
    "price_per_hour": "price_per_hour",
    "cpu_ghz": "cpu_ghz", "ghz": "cpu_ghz",
    "vram": "vram_gb", "vram_gb": "vram_gb",
    "cores": "cpu_cores", "cpu_cores": "cpu_cores",
    "ram": "ram_gb", "ram_gb": "ram_gb",
    "reliability": "reliability",
    "zcpu": "zcpu", "zgpu": "zgpu",
}


class CloreSeaError(Exception):
    """Raised when a Clore API call fails or returns unusable output."""


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_ghz(cpu_name: str) -> float:
    """Pull the clock from a Clore cpu string ('... @ 3.30GHz') -> 3.30."""
    m = re.search(r"@\s*([\d.]+)\s*GHz", cpu_name or "", re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


def _parse_disk_gb(disk: str) -> int:
    """Best-effort disk size in GB from a Clore disk string ('...47.3422GB')."""
    m = re.search(r"([\d.]+)\s*GB", disk or "", re.IGNORECASE)
    return int(float(m.group(1))) if m else 0


def _parse_cpus(cpus: object) -> tuple[int, int]:
    """'cores/threads' (e.g. '2/4') -> (cores, threads); tolerant of ints."""
    s = str(cpus or "")
    if "/" in s:
        a, _, b = s.partition("/")
        return _as_int(a), _as_int(b)
    n = _as_int(s)
    return n, n


class _CloreApi:
    """Thin REST wrapper: single ``auth`` token + browser UA, over a transport."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        transport: Transport,
        timeout_s: float,
    ) -> None:
        self._key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout_s

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: object = None,
    ) -> HttpResponse:
        try:
            resp = self._transport(
                method,
                self._base_url + path,
                headers={"auth": self._key, "User-Agent": _BROWSER_UA,
                         "Accept": "application/json"},
                params=params,
                json_body=body,
                timeout=self._timeout,
            )
        except TransportError as exc:
            raise CloreSeaError(f"Clore API unreachable: {exc}") from exc
        if not resp.ok:
            raise CloreSeaError(_error_message(resp, f"{method} {path}"))
        return resp

    def get_json(self, path: str, params: dict[str, str] | None = None) -> object:
        return self.request("GET", path, params=params).json()


def _error_message(resp: HttpResponse, action: str) -> str:
    """Clore errors are short ('error code: 1010' for Cloudflare, {code:N} JSON)."""
    detail = resp.text.strip()
    hint = ""
    if "1010" in detail:
        hint = " (Cloudflare blocked the user-agent)"
    elif resp.status_code == 429 or '"code":5' in detail.replace(" ", ""):
        hint = " (rate limited — Clore allows ~1 req/s)"
    return f"Clore {action} failed (HTTP {resp.status_code}): {detail}{hint}"


def _gpu_model(server: dict[str, object]) -> tuple[str, int]:
    """(model, count) from gpu_array (['RTX 3080', ...]) or the spec string."""
    arr = server.get("gpu_array")
    if isinstance(arr, list) and arr:
        return str(arr[0]), len(arr)
    specs = server.get("specs")
    gpu = str(specs.get("gpu", "")) if isinstance(specs, dict) else ""
    # "1x NVIDIA GeForce RTX 3080" -> count 1, model "GeForce RTX 3080"
    m = re.match(r"\s*(\d+)\s*x\s*(?:NVIDIA\s+)?(.+)", gpu, re.IGNORECASE)
    if m:
        return m.group(2).strip(), _as_int(m.group(1), 1)
    return gpu.replace("NVIDIA", "").strip(), 1 if gpu else 0


class CloreSea:
    """A hosting sea backed by the Clore.ai REST API (crypto-settled marketplace)."""

    is_marketplace = True  # dynamic, filterable catalog -> included in seas_search

    def __init__(
        self,
        name: str,
        *,
        api_key: str | None = None,
        api_key_env: str = "CLORE_API_KEY",
        base_url: str = _DEFAULT_BASE_URL,
        transport: Transport | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        ssh_user: str = "root",
        ssh_pubkey_path: str | None = None,
        pubkey_loader: Callable[[str], str] | None = None,
        default_image: str = _DEFAULT_IMAGE,
        default_currency: str = _DEFAULT_CURRENCY,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.name = name
        self.api_key_env = api_key_env
        self._api_key = api_key if api_key is not None else os.environ.get(api_key_env)
        self._base_url = base_url
        self._transport: Transport = transport or urllib_transport
        self._clock: Callable[[], float] = clock or time.time
        self._sleeper: Callable[[float], None] = sleeper or time.sleep
        self._ssh_user = ssh_user
        self._ssh_pubkey_path = ssh_pubkey_path
        self._pubkey_loader = pubkey_loader or _default_pubkey_loader
        self._default_image = default_image
        self._default_currency = default_currency
        self._timeout_s = timeout_s
        self._ready_timeout_s = ready_timeout_s
        self._poll_interval_s = poll_interval_s
        self._api: _CloreApi | None = None

    def _client(self) -> _CloreApi:
        if self._api_key is None:
            raise CloreSeaError(
                f"sea {self.name!r}: no Clore API key "
                f"(set ${self.api_key_env} in the marina environment / .env)"
            )
        if self._api is None:
            self._api = _CloreApi(
                self._api_key, self._base_url, self._transport, self._timeout_s
            )
        return self._api

    # ----- offers / search -----

    @staticmethod
    def _host_types(host_type: object) -> list[bool]:
        """Spot flags to include for a requested host_type. False = on-demand.

        Clore returns both prices per server in one /marketplace call, so 'any'
        needs no second request — it just emits both rows where each exists.
        """
        ht = str(host_type or "").strip().lower()
        if ht in ("", "on-demand", "ondemand", "on_demand", "od", "demand"):
            return [False]
        if ht in ("interruptible", "spot", "bid", "int", "preemptible"):
            return [True]
        if ht in ("any", "all", "both"):
            return [False, True]
        raise CloreSeaError(
            f"unknown host_type {host_type!r}; use 'on-demand', 'spot', or 'any'"
        )

    def _offer(self, server: dict[str, object], is_spot: bool) -> Offer | None:
        specs = server.get("specs") if isinstance(server.get("specs"), dict) else {}
        assert isinstance(specs, dict)
        usd = {}
        price = server.get("price")
        if isinstance(price, dict) and isinstance(price.get("usd"), dict):
            usd = price["usd"]
        on_demand = _as_float(usd.get("on_demand_usd"))
        spot = _as_float(usd.get("spot"))
        # A server only offers spot if its spot price is > 0; skip otherwise.
        if is_spot and spot <= 0:
            return None
        dollars = spot if is_spot else on_demand
        if dollars <= 0:
            return None

        gpu_model, gpu_count = _gpu_model(server)
        vram = _as_int(specs.get("gpuram"))
        cpu_name = str(specs.get("cpu", ""))
        cores, threads = _parse_cpus(specs.get("cpus"))
        ghz = _parse_ghz(cpu_name)
        net = specs.get("net") if isinstance(specs.get("net"), dict) else {}
        assert isinstance(net, dict)
        server_id = _as_int(server.get("id"))
        offer_id = f"{server_id}" + ("#spot" if is_spot else "")
        return Offer(
            sea=self.name,
            offer_id=offer_id,
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            vram_gb=vram,
            fp64_native=fp64_native(gpu_model),
            cpu_ghz=ghz,
            cpu_cores=cores,
            cpu_name=cpu_name,
            cpu_cores_total=threads,
            ram_gb=_as_int(specs.get("ram")),
            disk_gb=_parse_disk_gb(str(specs.get("disk", ""))),
            price_per_hour=dollars,
            host_type="interruptible" if is_spot else "on-demand",
            reliability=_as_float(server.get("reliability"), None)  # type: ignore[arg-type]
            if server.get("reliability") is not None else None,
            inet_down_mbps=_as_float(net.get("down"), None)  # type: ignore[arg-type]
            if net.get("down") is not None else None,
            geolocation=str(net.get("cc", "")),
            cuda_max_good=_as_float(server.get("cuda_version")),
            # zGPU from datasheet FP64 + datasheet VRAM bandwidth (Clore gives no
            # measured bandwidth); zCPU is real (Clore exposes CPU clock + model).
            zgpu=zgpu(gpu_model, gpu_count, gpu_mem_bandwidth(gpu_model, vram), 0.0),
            zcpu=zcpu(cores, threads, ghz, cpu_name),
            extras={"server_id": server_id, "on_demand_usd": on_demand,
                    "spot_usd": spot, "allowed_coins": server.get("allowed_coins")},
        )

    def search(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 20,
    ) -> list[Offer]:
        f = dict(filters or {})
        spot_flags = self._host_types(f.get("host_type"))
        data = self._client().get_json("/marketplace")
        servers = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(servers, list):
            raise CloreSeaError("Clore /marketplace: expected a 'servers' array")

        offers: list[Offer] = []
        for raw in servers:
            if not isinstance(raw, dict) or raw.get("rented"):
                continue  # rented servers can't be ordered
            for is_spot in spot_flags:
                offer = self._offer(raw, is_spot)
                if offer is not None:
                    offers.append(offer)

        # Shared Vast-parity filter set: gpu_name / cpu_name (family-aware, incl.
        # "trpro") / vram_gb / min_cuda / min_reliability / max_dph. Clore exposes
        # CPU model, reliability, and CUDA, so all of these are meaningful here.
        offers = apply_common_filters(offers, f)

        order = str(f.get("order") or "").strip()
        if order:
            offers = self._sort_offers(offers, order)
        else:
            offers = sorted(offers, key=lambda o: o.price_per_hour)
        return offers[:limit]

    @staticmethod
    def _sort_offers(offers: list[Offer], order: str) -> list[Offer]:
        desc = order.startswith("-")
        attr = _ORDER_KEYS.get((order[1:] if desc else order).lower())
        if attr is None:
            raise CloreSeaError(
                f"unknown sort key {order!r}; known: {sorted(set(_ORDER_KEYS))}"
            )

        def keyfn(o: Offer) -> float:
            val = getattr(o, attr)
            return float(val) if val is not None else float("-inf")

        return sorted(offers, key=keyfn, reverse=desc)

    def recommend(
        self,
        workload: str,
        budget_per_hour: float | None = None,
        min_hours: int | None = None,
    ) -> list[Offer]:
        del workload, budget_per_hour, min_hours
        raise NotImplementedError(
            f"sea {self.name!r} (clore): workload presets not implemented; "
            f"use sea_search with explicit filters"
        )

    # ----- lifecycle: create (host_create) -----

    @staticmethod
    def _parse_offer_id(offer_id: str) -> tuple[int, bool]:
        """'<server_id>[#spot]' -> (server_id, is_spot)."""
        is_spot = offer_id.endswith("#spot")
        core = offer_id[:-5] if is_spot else offer_id
        try:
            return int(core), is_spot
        except ValueError as exc:
            raise CloreSeaError(
                f"clore offer_id {offer_id!r} must be '<server_id>[#spot]'"
                f" (from sea_search)"
            ) from exc

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
        del disk_gb  # Clore allocates the server's own disk; no per-order size
        if not offer_id:
            raise CloreSeaError("clore create requires offer_id (from sea_search)")
        server_id, is_spot = self._parse_offer_id(offer_id)
        if not self._ssh_pubkey_path:
            raise CloreSeaError(
                f"sea {self.name!r}: ssh_pubkey_path is required for host_create"
            )
        pubkey = self._pubkey_loader(self._ssh_pubkey_path).strip()
        if not pubkey:
            raise CloreSeaError(f"empty ssh public key at {self._ssh_pubkey_path!r}")

        body: dict[str, object] = {
            "currency": self._default_currency,
            "image": image or self._default_image,
            "renting_server": server_id,
            "type": "spot" if is_spot else "on-demand",
            "ssh_key": pubkey,
            "autossh_entrypoint": True,  # let Clore stand up sshd for our tunnel
        }
        if is_spot:
            # Bid at the current spot price the marketplace advertised for it.
            body["spotprice"] = self._spot_price_for(server_id)
        if onstart:
            body["command"] = onstart
        if env:
            body["env"] = env

        self._client().request("POST", "/create_order", body=body)
        return self._wait_until_ready(server_id, name)

    def _spot_price_for(self, server_id: int) -> float:
        data = self._client().get_json("/marketplace")
        servers = data.get("servers") if isinstance(data, dict) else []
        for raw in servers or []:
            if isinstance(raw, dict) and _as_int(raw.get("id")) == server_id:
                price = raw.get("price")
                if isinstance(price, dict) and isinstance(price.get("usd"), dict):
                    return _as_float(price["usd"].get("spot"))
        raise CloreSeaError(f"clore server {server_id} no longer offers a spot price")

    def _find_order(self, server_id: int) -> dict[str, object] | None:
        data = self._client().get_json("/my_orders")
        orders = data.get("orders") if isinstance(data, dict) else None
        if not isinstance(orders, list):
            return None
        for order in orders:
            if not isinstance(order, dict):
                continue
            # Clore reports the rented server under a few possible keys.
            sid = order.get("server_id", order.get("renting_server", order.get("si")))
            if _as_int(sid) == server_id:
                return order
        return None

    def _wait_until_ready(self, server_id: int, name: str | None) -> HostHandle:
        host_name = name or f"{self.name}-{server_id}"
        deadline = self._clock() + self._ready_timeout_s
        while True:
            order = self._find_order(server_id)
            if order is not None:
                ip = order.get("pub_ip", order.get("ip"))
                if ip:  # provisioned: we have a reachable address
                    return self._handle(host_name, order, server_id, str(ip))
            if self._clock() >= deadline:
                raise CloreSeaError(
                    f"clore order for server {server_id} not ready after "
                    f"{self._ready_timeout_s:.0f}s"
                )
            self._sleeper(self._poll_interval_s)

    def _handle(
        self, host_name: str, order: dict[str, object], server_id: int, ip: str
    ) -> HostHandle:
        # SSH port: Clore maps 22 to a forwarded port; expose it if present.
        ssh_port = _as_int(order.get("ssh_port"), 22) or 22
        return HostHandle(
            name=host_name,
            sea=self.name,
            instance_id=str(order.get("id", server_id)),
            grpc_target=f"{ip}:50051",
            state="running",
            cost_per_hour=_as_float(order.get("price")),
            created_at_unix_ms=int(self._clock() * 1000),
            ssh_target=f"{self._ssh_user}@{ip}:{ssh_port}",
        )

    # ----- lifecycle: list / destroy / (stop/start/renew unsupported) -----

    def list_instances(
        self, state_filter: str | None = None
    ) -> list[HostHandle]:
        data = self._client().get_json("/my_orders")
        orders = data.get("orders") if isinstance(data, dict) else None
        handles: list[HostHandle] = []
        if isinstance(orders, list):
            for order in orders:
                if not isinstance(order, dict):
                    continue
                ip = str(order.get("pub_ip", order.get("ip", "")) or "")
                sid = _as_int(order.get("server_id", order.get("renting_server")))
                name = f"{self.name}-{sid}"
                handles.append(self._handle(name, order, sid, ip))
        if state_filter is not None:
            handles = [h for h in handles if h.state == state_filter]
        return handles

    def destroy(self, host_name: str, *, kill_running: bool = False) -> None:
        del kill_running
        order_id = self._find_order_id(host_name)
        self._client().request(
            "POST", "/cancel_order", body={"id": order_id, "issue": "marina destroy"}
        )

    def _find_order_id(self, host_name: str) -> int:
        data = self._client().get_json("/my_orders")
        orders = data.get("orders") if isinstance(data, dict) else None
        if isinstance(orders, list):
            for order in orders:
                if not isinstance(order, dict):
                    continue
                sid = _as_int(order.get("server_id", order.get("renting_server")))
                if host_name in (f"{self.name}-{sid}", str(order.get("id"))):
                    return _as_int(order.get("id"))
        raise CloreSeaError(f"clore: no order matching {host_name!r}")

    def stop(self, host_name: str) -> None:
        del host_name
        raise NotImplementedError(f"sea {self.name!r} (clore): stop is not supported")

    def start(self, host_name: str) -> None:
        del host_name
        raise NotImplementedError(f"sea {self.name!r} (clore): start is not supported")

    def renew(self, host_name: str, hours: int) -> None:
        del host_name, hours
        raise NotImplementedError(
            f"sea {self.name!r} (clore): orders run until cancelled; nothing to renew"
        )

    # ----- status / cost -----

    def status(self) -> SeaStatus:
        if self._api_key is None:
            return SeaStatus(
                sea=self.name,
                reachable=False,
                detail=f"no API key (set ${self.api_key_env})",
            )
        try:
            self._client().get_json("/my_orders")
        except CloreSeaError as exc:
            return SeaStatus(sea=self.name, reachable=False, detail=str(exc))
        return SeaStatus(sea=self.name, reachable=True, detail="clore reachable")

    def cost_summary(self) -> CostBreakdown:
        return CostBreakdown(
            sea=self.name, total_per_hour=0.0, per_host=(), balance=None
        )


def _default_pubkey_loader(path: str) -> str:
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return f.read().strip()
