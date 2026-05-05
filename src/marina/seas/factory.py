from __future__ import annotations

from typing import Any

from marina.seas.base import Sea
from marina.seas.docker_sea import DockerSea, DockerSeaCapability


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
