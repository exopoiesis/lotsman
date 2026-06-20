"""VerdaSea — a hosting sea backed by the Verda Cloud REST API (ex-DataCrunch).

A second marketplace alongside VastSea, kept as a backup channel (EU regions,
fixed-price on-demand, native spot). Unlike VastSea (which shells out to the
`vastai` CLI) Verda has no CLI, so this talks the REST API directly over an
injectable HTTP transport — the default uses the Python stdlib (`urllib`), so
no new third-party dependency, and tests script responses without a network.

Credentials, like VastSea's API key, never sit in the TOML: the client id /
secret are read from the marina process environment (a gitignored ``.env`` next
to the config, loaded at startup), named by ``client_id_env`` /
``client_secret_env`` (default ``VERDA_CLIENT_ID`` / ``VERDA_CLIENT_SECRET``).

API shape mirrored from SkyPilot's Verda provider:
  - OAuth2 client-credentials: POST ``/oauth2/token`` -> bearer access_token.
  - ``GET /instance-types``            — catalog (specs + price + spot_price).
  - ``GET /instance-availability``     — which types are up per region (?is_spot).
  - ``GET /ssh-keys`` / ``POST /ssh-keys`` — key management for provisioning.
  - ``POST /instances``                — create (returns the new instance id).
  - ``GET /instances`` / ``/instances/{id}`` — list / poll.
  - ``PUT /instances`` ``{id:[...],action}``  — lifecycle actions (delete...).

Scope (this pass): the two commands Marina actually needs to evaluate Verda as
a backup — ``search`` (sea_search) and ``create`` (host_create) — plus the
minimum of the Sea protocol around them (status / list / destroy). A host's
gRPC tunnel wiring is a later concern, as in VastSea's own staged scope;
``create`` returns a usable ``ssh_target`` (root@ip) for manual DFT runs.
"""
from __future__ import annotations

import json
import os
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
from marina.seas.perf_score import gpu_mem_bandwidth, zgpu
from marina.seas.vast_sea import fp64_native

# Re-exported for back-compat: tests and callers import these from verda_sea.
__all__ = ["VerdaSea", "VerdaSeaError", "HttpResponse", "Transport"]

_DEFAULT_BASE_URL = "https://api.verda.com/v1"
_DEFAULT_REGION = "FIN-03"
_TOKEN_ENDPOINT = "/oauth2/token"
_DEFAULT_TIMEOUT_S = 45.0
_TOKEN_SKEW_S = 60.0  # refresh this many seconds before the token actually expires
_DEFAULT_DISK_GB = 100
_DEFAULT_IMAGE = "ubuntu-24.04-cuda-12.8-open-docker"
_DEFAULT_READY_TIMEOUT_S = 1800.0
_DEFAULT_POLL_INTERVAL_S = 10.0

# Statuses (Verda) that mean the instance will never come up on its own.
_TERMINAL_BAD = {"error", "offline"}
# Verda status -> our HostHandle.state vocabulary.
_STATE_MAP = {
    "running": "running",
    "provisioning": "loading",
    "ordered": "loading",
    "restoring": "loading",
    "offline": "stopped",
    "hibernating": "stopped",
    "error": "error",
}

# Offer sort keys -> Offer attribute (subset that's meaningful for Verda data).
_ORDER_KEYS = {
    "price": "price_per_hour", "dph": "price_per_hour",
    "price_per_hour": "price_per_hour",
    "vram": "vram_gb", "vram_gb": "vram_gb",
    "cores": "cpu_cores", "cpu_cores": "cpu_cores",
    "ram": "ram_gb", "ram_gb": "ram_gb",
    "disk": "disk_gb", "disk_gb": "disk_gb",
    "zgpu": "zgpu",
}


class VerdaSeaError(Exception):
    """Raised when a Verda API call fails or returns unusable output."""


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


def _resolve_credentials(
    client_id: str | None,
    client_secret: str | None,
    id_env: str,
    secret_env: str,
) -> tuple[str | None, str | None, str, str]:
    """Resolve (id, secret, base_url, region) from explicit args, else env.

    Mirrors VastSea: the secret never sits in the TOML — it lives in the
    process environment (a gitignored ``.env`` next to the config, loaded at
    startup), named by ``id_env`` / ``secret_env``. Returns Nones for missing
    creds so status() can report it cleanly. base_url / region may also be
    overridden via env, else fall back to the Verda defaults.
    """
    base_url = os.environ.get("VERDA_BASE_URL", _DEFAULT_BASE_URL)
    region = os.environ.get("VERDA_DEFAULT_REGION", _DEFAULT_REGION)
    cid = client_id or os.environ.get(id_env)
    csecret = client_secret or os.environ.get(secret_env)
    return cid, csecret, base_url, region


class _VerdaApi:
    """OAuth2 token management + thin REST wrapper over an injectable transport."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        transport: Transport,
        clock: Callable[[], float],
        timeout_s: float,
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._clock = clock
        self._timeout = timeout_s
        self._token: str | None = None
        self._expires_at = 0.0

    def _authenticate(self) -> None:
        try:
            resp = self._transport(
                "POST",
                self._base_url + _TOKEN_ENDPOINT,
                headers={"Content-Type": "application/json"},
                json_body={
                    "grant_type": "client_credentials",
                    "client_id": self._id,
                    "client_secret": self._secret,
                },
                timeout=self._timeout,
            )
        except TransportError as exc:
            raise VerdaSeaError(f"Verda API unreachable: {exc}") from exc
        if not resp.ok:
            raise VerdaSeaError(_error_message(resp, "authenticate"))
        data = resp.json()
        if not isinstance(data, dict) or "access_token" not in data:
            raise VerdaSeaError("Verda auth returned no access_token")
        self._token = str(data["access_token"])
        self._expires_at = self._clock() + _as_float(data.get("expires_in"), 3600.0)

    def _ensure_token(self) -> None:
        if self._token is None or self._clock() >= self._expires_at - _TOKEN_SKEW_S:
            self._authenticate()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: object = None,
    ) -> HttpResponse:
        self._ensure_token()
        resp = self._call(method, path, params, body)
        if resp.status_code == 401:  # token rejected -> re-auth once and retry
            self._authenticate()
            resp = self._call(method, path, params, body)
        if not resp.ok:
            raise VerdaSeaError(_error_message(resp, f"{method} {path}"))
        return resp

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None,
        body: object,
    ) -> HttpResponse:
        try:
            return self._transport(
                method,
                self._base_url + path,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": "marina-verda",
                },
                params=params,
                json_body=body,
                timeout=self._timeout,
            )
        except TransportError as exc:
            raise VerdaSeaError(f"Verda API unreachable: {exc}") from exc

    def get_json(self, path: str, params: dict[str, str] | None = None) -> object:
        return self.request("GET", path, params=params).json()


def _error_message(resp: HttpResponse, action: str) -> str:
    """Build a readable error from a Verda error body ({code,message})."""
    detail = resp.text.strip()
    try:
        data = json.loads(detail)
        if isinstance(data, dict):
            code = data.get("code", "")
            msg = data.get("message", detail)
            detail = f"{code}: {msg}" if code else str(msg)
    except (ValueError, TypeError):
        pass
    return f"Verda {action} failed (HTTP {resp.status_code}): {detail}"


def _extract_gpu_model(instance: dict[str, object]) -> str:
    """GPU model from the instance-type dict (``model`` or the GPU description)."""
    model = instance.get("model")
    if model:
        return str(model)
    gpu = instance.get("gpu") or {}
    desc = str(gpu.get("description", "")) if isinstance(gpu, dict) else ""
    # "1x NVIDIA A100 SXM4 80GB" -> "A100 SXM4"; best-effort, keep it simple.
    cleaned = desc.replace("NVIDIA", "").strip()
    return cleaned


class VerdaSea:
    """A hosting sea backed by the Verda Cloud REST API."""

    is_marketplace = True  # dynamic, filterable catalog -> included in seas_search

    def __init__(
        self,
        name: str,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        client_id_env: str = "VERDA_CLIENT_ID",
        client_secret_env: str = "VERDA_CLIENT_SECRET",
        base_url: str | None = None,
        default_region: str | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        ssh_user: str = "root",
        ssh_pubkey_path: str | None = None,
        pubkey_loader: Callable[[str], str] | None = None,
        default_image: str = _DEFAULT_IMAGE,
        default_disk_gb: int = _DEFAULT_DISK_GB,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.name = name
        self._id_env = client_id_env
        self._secret_env = client_secret_env
        cid, csecret, cfg_url, cfg_region = _resolve_credentials(
            client_id, client_secret, client_id_env, client_secret_env
        )
        self._client_id = cid
        self._client_secret = csecret
        self._base_url = base_url or cfg_url
        self._region = default_region or cfg_region
        self._transport: Transport = transport or urllib_transport
        self._clock: Callable[[], float] = clock or time.time
        self._sleeper: Callable[[float], None] = sleeper or time.sleep
        self._ssh_user = ssh_user
        self._ssh_pubkey_path = ssh_pubkey_path
        self._pubkey_loader = pubkey_loader or _default_pubkey_loader
        self._default_image = default_image
        self._default_disk_gb = default_disk_gb
        self._timeout_s = timeout_s
        self._ready_timeout_s = ready_timeout_s
        self._poll_interval_s = poll_interval_s
        self._api: _VerdaApi | None = None

    # ----- api access -----

    def _client(self) -> _VerdaApi:
        if self._client_id is None or self._client_secret is None:
            raise VerdaSeaError(
                f"sea {self.name!r}: no Verda credentials "
                f"(set ${self._id_env} / ${self._secret_env} in the marina "
                f"environment / .env)"
            )
        if self._api is None:
            self._api = _VerdaApi(
                self._client_id,
                self._client_secret,
                self._base_url,
                self._transport,
                self._clock,
                self._timeout_s,
            )
        return self._api

    # ----- offers / search -----

    @staticmethod
    def _host_types(host_type: object) -> list[bool]:
        """Spot flags to query for a requested host_type. False = on-demand."""
        ht = str(host_type or "").strip().lower()
        if ht in ("", "on-demand", "ondemand", "on_demand", "od", "demand"):
            return [False]
        if ht in ("interruptible", "spot", "bid", "int", "preemptible"):
            return [True]
        if ht in ("any", "all", "both"):
            return [False, True]
        raise VerdaSeaError(
            f"unknown host_type {host_type!r}; use 'on-demand', 'spot', or 'any'"
        )

    def _availability(self, is_spot: bool) -> dict[str, set[str]]:
        """instance_type -> set of region codes available for this pricing mode."""
        data = self._client().get_json(
            "/instance-availability",
            params={"is_spot": "true" if is_spot else "false"},
        )
        out: dict[str, set[str]] = {}
        if not isinstance(data, list):
            return out
        for loc in data:
            if not isinstance(loc, dict):
                continue
            region = str(loc.get("location_code", ""))
            for itype in loc.get("availabilities", []) or []:
                out.setdefault(str(itype), set()).add(region)
        return out

    def _offer(self, raw: dict[str, object], region: str, is_spot: bool) -> Offer:
        itype = str(raw.get("instance_type", ""))
        cpu = raw.get("cpu") or {}
        mem = raw.get("memory") or {}
        gpu = raw.get("gpu") or {}
        gpu_mem = raw.get("gpu_memory") or {}
        num_gpus = _as_int(gpu.get("number_of_gpus")) if isinstance(gpu, dict) else 0
        total_vram = (
            _as_int(gpu_mem.get("size_in_gigabytes")) if isinstance(gpu_mem, dict)
            else 0
        )
        vram_per_gpu = total_vram // num_gpus if num_gpus > 0 else 0
        gpu_model = _extract_gpu_model(raw) if num_gpus > 0 else ""
        on_demand = _as_float(raw.get("price_per_hour"))
        spot = _as_float(raw.get("spot_price") or raw.get("spot_price_per_hour"))
        price = (spot if spot > 0 else on_demand) if is_spot else on_demand
        # offer_id is self-contained so host_create needs only this one token:
        # "<instance_type>@<region>" (+ "#spot" for an interruptible order).
        offer_id = f"{itype}@{region}" + ("#spot" if is_spot else "")
        return Offer(
            sea=self.name,
            offer_id=offer_id,
            gpu_model=gpu_model,
            gpu_count=num_gpus,
            vram_gb=vram_per_gpu,
            fp64_native=fp64_native(gpu_model),
            cpu_ghz=0.0,  # Verda catalog does not advertise CPU clock
            cpu_cores=_as_int(cpu.get("number_of_cores")) if isinstance(cpu, dict)
            else 0,
            cpu_name="",  # nor the CPU model -> zCPU left unscored (0)
            cpu_cores_total=_as_int(cpu.get("number_of_cores"))
            if isinstance(cpu, dict) else 0,
            ram_gb=_as_int(mem.get("size_in_gigabytes")) if isinstance(mem, dict)
            else 0,
            disk_gb=0,  # disk is chosen at create time, not fixed by the type
            price_per_hour=price,
            host_type="interruptible" if is_spot else "on-demand",
            geolocation=region,
            # zGPU from datasheet FP64 + datasheet VRAM bandwidth (Verda gives no
            # measured bandwidth); zCPU/dlperf stay 0 — inputs aren't advertised.
            zgpu=zgpu(gpu_model, num_gpus, gpu_mem_bandwidth(gpu_model, vram_per_gpu),
                      0.0),
            extras={"instance_type": itype, "spot_price": spot,
                    "on_demand_price": on_demand},
        )

    def search(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 20,
    ) -> list[Offer]:
        f = dict(filters or {})
        spot_flags = self._host_types(f.get("host_type"))
        types = self._client().get_json("/instance-types")
        if not isinstance(types, list):
            raise VerdaSeaError("Verda /instance-types: expected a JSON array")
        offers: list[Offer] = []
        for is_spot in spot_flags:
            avail = self._availability(is_spot)
            for raw in types:
                if not isinstance(raw, dict):
                    continue
                itype = str(raw.get("instance_type", ""))
                for region in sorted(avail.get(itype, ())):
                    offers.append(self._offer(raw, region, is_spot))

        # Shared Vast-parity filter set (gpu_name / cpu_name family-aware /
        # vram_gb / min_cuda / min_reliability / max_dph). Verda exposes no CPU
        # model, reliability, or CUDA, so those constraints exclude its offers.
        offers = [o for o in offers if o.price_per_hour > 0]
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
            raise VerdaSeaError(
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
            f"sea {self.name!r} (verda): workload presets not implemented; "
            f"use sea_search with explicit filters"
        )

    # ----- lifecycle: create (host_create) -----

    @staticmethod
    def _parse_offer_id(offer_id: str) -> tuple[str, str, bool]:
        """'<instance_type>@<region>[#spot]' -> (instance_type, region, is_spot)."""
        is_spot = offer_id.endswith("#spot")
        core = offer_id[:-5] if is_spot else offer_id
        if "@" not in core:
            raise VerdaSeaError(
                f"verda offer_id {offer_id!r} must be '<instance_type>@<region>'"
                f" (from sea_search)"
            )
        itype, region = core.rsplit("@", 1)
        if not itype or not region:
            raise VerdaSeaError(f"verda offer_id {offer_id!r} is malformed")
        return itype, region, is_spot

    def _ensure_ssh_key_id(self) -> str:
        """Return the Verda ssh-key id for our local public key (create if new)."""
        if not self._ssh_pubkey_path:
            raise VerdaSeaError(
                f"sea {self.name!r}: ssh_pubkey_path is required for host_create"
            )
        pubkey = self._pubkey_loader(self._ssh_pubkey_path).strip()
        if not pubkey:
            raise VerdaSeaError(f"empty ssh public key at {self._ssh_pubkey_path!r}")
        existing = self._client().get_json("/ssh-keys")
        if isinstance(existing, list):
            for key in existing:
                if isinstance(key, dict) and str(key.get("key", "")).strip() == pubkey:
                    return str(key.get("id"))
        # Not registered yet -> create it. POST returns the new id as plain text.
        resp = self._client().request(
            "POST", "/ssh-keys",
            body={"name": f"marina-{self.name}", "key": pubkey},
        )
        return resp.text.strip().strip('"')

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
        del onstart, env  # Verda runs an OS image; no container onstart/-e hooks
        if not offer_id:
            raise VerdaSeaError("verda create requires offer_id (from sea_search)")
        instance_type, region, is_spot = self._parse_offer_id(offer_id)
        host_name = name or f"{self.name}-{instance_type}".replace(".", "-")
        ssh_key_id = self._ensure_ssh_key_id()
        payload = {
            "instance_type": instance_type,
            "hostname": host_name,
            "location_code": region,
            "is_spot": is_spot,
            "contract": "SPOT" if is_spot else "PAY_AS_YOU_GO",
            "image": image or self._default_image,
            "description": "Created by Marina",
            "ssh_key_ids": [ssh_key_id],
            "os_volume": {
                "name": host_name,
                "size": disk_gb or self._default_disk_gb,
            },
        }
        resp = self._client().request("POST", "/instances", body=payload)
        instance_id = resp.text.strip().strip('"')
        entry = self._wait_until_ready(instance_id)
        return self._handle_from_instance(host_name, entry)

    def _wait_until_ready(self, instance_id: str) -> dict[str, object]:
        deadline = self._clock() + self._ready_timeout_s
        while True:
            entry = self._client().get_json(f"/instances/{instance_id}")
            if not isinstance(entry, dict):
                raise VerdaSeaError(
                    f"verda /instances/{instance_id}: expected an object"
                )
            status = str(entry.get("status", ""))
            if status == "running":
                return entry
            if status in _TERMINAL_BAD:
                raise VerdaSeaError(
                    f"verda instance {instance_id} reached terminal status "
                    f"{status!r}"
                )
            if self._clock() >= deadline:
                raise VerdaSeaError(
                    f"verda instance {instance_id} not ready after "
                    f"{self._ready_timeout_s:.0f}s (last status {status!r})"
                )
            self._sleeper(self._poll_interval_s)

    def _handle_from_instance(
        self, host_name: str, entry: dict[str, object]
    ) -> HostHandle:
        ip = entry.get("ip")
        if isinstance(ip, list):
            ip = ip[0] if ip else None
        status = str(entry.get("status", ""))
        ssh_target = f"{self._ssh_user}@{ip}:22" if ip else None
        grpc_target = f"{ip}:50051" if ip else ""
        return HostHandle(
            name=host_name,
            sea=self.name,
            instance_id=str(entry.get("id", "")),
            grpc_target=grpc_target,
            state=_STATE_MAP.get(status, status or "unknown"),
            cost_per_hour=0.0,  # not returned per-instance by Verda
            created_at_unix_ms=int(self._clock() * 1000),
            ssh_target=ssh_target,
        )

    # ----- lifecycle: list / destroy / (stop/start/renew unsupported) -----

    def list_instances(
        self, state_filter: str | None = None
    ) -> list[HostHandle]:
        data = self._client().get_json("/instances")
        handles: list[HostHandle] = []
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("hostname", entry.get("id", "")))
                handles.append(self._handle_from_instance(name, entry))
        if state_filter is not None:
            handles = [h for h in handles if h.state == state_filter]
        return handles

    def _find_instance_id(self, host_name: str) -> str:
        data = self._client().get_json("/instances")
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if host_name in (str(entry.get("hostname", "")), str(entry.get("id"))):
                    return str(entry.get("id"))
        raise VerdaSeaError(f"verda: no instance matching {host_name!r}")

    def destroy(self, host_name: str, *, kill_running: bool = False) -> None:
        del kill_running
        instance_id = self._find_instance_id(host_name)
        self._client().request(
            "PUT", "/instances",
            body={"id": [instance_id], "action": "delete"},
        )

    def stop(self, host_name: str) -> None:
        del host_name
        raise NotImplementedError(
            f"sea {self.name!r} (verda): stop is not supported"
        )

    def start(self, host_name: str) -> None:
        del host_name
        raise NotImplementedError(
            f"sea {self.name!r} (verda): start is not supported"
        )

    def renew(self, host_name: str, hours: int) -> None:
        del host_name, hours
        raise NotImplementedError(
            f"sea {self.name!r} (verda): instances run until destroyed; "
            f"there is no rental to renew"
        )

    # ----- status / cost -----

    def status(self) -> SeaStatus:
        if self._client_id is None or self._client_secret is None:
            return SeaStatus(
                sea=self.name,
                reachable=False,
                detail=f"no credentials (set ${self._id_env}/${self._secret_env})",
            )
        try:
            # A cheap authenticated call proves reachability + valid credentials.
            self._client().get_json("/instances")
        except VerdaSeaError as exc:
            return SeaStatus(sea=self.name, reachable=False, detail=str(exc))
        return SeaStatus(sea=self.name, reachable=True, detail="verda reachable")

    def cost_summary(self) -> CostBreakdown:
        # Verda does not return per-instance price via /instances; report the
        # live host count's cost as unknown (0) rather than guess.
        return CostBreakdown(
            sea=self.name, total_per_hour=0.0, per_host=(), balance=None
        )


def _default_pubkey_loader(path: str) -> str:
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return f.read().strip()
