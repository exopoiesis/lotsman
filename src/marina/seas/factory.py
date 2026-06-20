from __future__ import annotations

from typing import Any

from marina.seas.base import Sea
from marina.seas.clore_sea import CloreSea
from marina.seas.docker_sea import DockerSea, DockerSeaCapability
from marina.seas.vast_sea import VastSea
from marina.seas.verda_sea import VerdaSea


class SeaConfigError(Exception):
    """Raised when a [seas.NAME] TOML section is invalid."""


_DOCKER_CAP_REQUIRED = (
    "gpu_model",
    "gpu_count",
    "vram_gb",
    "fp64_native",
    "cpu_ghz",
    "cpu_cores",
    "ram_gb",
    "disk_gb",
)
_DOCKER_CAP_OPTIONAL = ("price_per_hour", "reliability", "inet_down_mbps")


def build_sea(name: str, type_: str, raw: dict[str, Any]) -> Sea:
    """Construct a Sea instance from a TOML section.

    `raw` is the full section dict (including `type`, `docker_context`, and
    capability fields). Unknown fields are tolerated for forward-compat.
    """
    if type_ == "docker_sea":
        return _build_docker_sea(name, raw)
    if type_ == "vast_sea":
        return _build_vast_sea(name, raw)
    if type_ == "verda_sea":
        return _build_verda_sea(name, raw)
    if type_ == "clore_sea":
        return _build_clore_sea(name, raw)
    raise SeaConfigError(f"unknown sea type: {type_!r} for sea {name!r}")


def _build_docker_sea(name: str, raw: dict[str, Any]) -> DockerSea:
    docker_context = raw.get("docker_context")
    if not docker_context:
        raise SeaConfigError(
            f"sea {name!r} (docker_sea) requires 'docker_context'"
        )

    missing = [f for f in _DOCKER_CAP_REQUIRED if f not in raw]
    if missing:
        raise SeaConfigError(
            f"sea {name!r} (docker_sea) missing capability fields: {missing}"
        )

    cap_kwargs: dict[str, Any] = {f: raw[f] for f in _DOCKER_CAP_REQUIRED}
    for f in _DOCKER_CAP_OPTIONAL:
        if f in raw:
            cap_kwargs[f] = raw[f]

    capability = DockerSeaCapability(**cap_kwargs)
    return DockerSea(name, docker_context=docker_context, capability=capability)


def _build_vast_sea(name: str, raw: dict[str, Any]) -> VastSea:
    """Construct a VastSea from a `[seas.NAME]` TOML section.

    The API key never sits in the TOML in plaintext: config names an env var
    (`api_key_env`, default `VAST_API_KEY`) that VastSea reads at construction.
    """
    kwargs: dict[str, Any] = {"api_key_env": raw.get("api_key_env", "VAST_API_KEY")}
    for str_field in ("ssh_user", "ssh_key_path", "ssh_pubkey_path", "vastai_bin"):
        if str_field in raw:
            kwargs[str_field] = raw[str_field]
    if "container_grpc_port" in raw:
        kwargs["container_grpc_port"] = int(raw["container_grpc_port"])
    for float_field in (
        "ready_timeout_s", "poll_interval_s", "ssh_ready_timeout_s", "cmd_timeout_s"
    ):
        if float_field in raw:
            kwargs[float_field] = float(raw[float_field])
    return VastSea(name, **kwargs)


def _build_verda_sea(name: str, raw: dict[str, Any]) -> VerdaSea:
    """Construct a VerdaSea from a `[seas.NAME]` TOML section.

    Credentials never sit in the TOML (mirrors vast_sea): the section names the
    env vars (`client_id_env` / `client_secret_env`, default VERDA_CLIENT_ID /
    VERDA_CLIENT_SECRET) that VerdaSea reads from the process env / `.env` at
    construction. The TOML may pin region / ssh key / disk / image.
    """
    kwargs: dict[str, Any] = {}
    for str_field in (
        "client_id_env", "client_secret_env",
        "base_url", "default_region", "ssh_user", "ssh_pubkey_path",
        "default_image",
    ):
        if str_field in raw:
            kwargs[str_field] = raw[str_field]
    if "default_disk_gb" in raw:
        kwargs["default_disk_gb"] = int(raw["default_disk_gb"])
    for float_field in ("timeout_s", "ready_timeout_s", "poll_interval_s"):
        if float_field in raw:
            kwargs[float_field] = float(raw[float_field])
    return VerdaSea(name, **kwargs)


def _build_clore_sea(name: str, raw: dict[str, Any]) -> CloreSea:
    """Construct a CloreSea from a `[seas.NAME]` TOML section.

    Like vast_sea, the token never sits in the TOML: the section names the env
    var (`api_key_env`, default CLORE_API_KEY) that CloreSea reads from the
    process env / `.env`. The TOML may pin ssh key / image / currency.
    """
    kwargs: dict[str, Any] = {"api_key_env": raw.get("api_key_env", "CLORE_API_KEY")}
    for str_field in (
        "base_url", "ssh_user", "ssh_pubkey_path", "default_image", "default_currency"
    ):
        if str_field in raw:
            kwargs[str_field] = raw[str_field]
    for float_field in ("timeout_s", "ready_timeout_s", "poll_interval_s"):
        if float_field in raw:
            kwargs[float_field] = float(raw[float_field])
    return CloreSea(name, **kwargs)
