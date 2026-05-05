from __future__ import annotations

try:
    import tomllib  # Python 3.11+ stdlib
except ImportError:  # pragma: no cover — 3.10 fallback for older base images
    import tomli as tomllib  # type: ignore[no-redef]

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HostConfig:
    name: str
    target: str  # gRPC target, e.g., "localhost:50051" or "unix:///tmp/...sock"


@dataclass
class SeaConfig:
    """Raw, type-tagged TOML section for a sea provider.

    The factory (`marina.seas.factory.build_sea`) consumes this and produces a
    concrete Sea instance. Keeping config separate from instantiation lets us
    parse + validate without spawning subprocesses.
    """

    name: str
    type: str  # e.g. "docker_sea"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarinaConfig:
    hosts: list[HostConfig] = field(default_factory=list)
    seas: list[SeaConfig] = field(default_factory=list)


def load_config(path: Path | None) -> MarinaConfig:
    if path is None or not path.exists():
        return MarinaConfig()

    with path.open("rb") as f:
        data = tomllib.load(f)

    hosts_block = data.get("hosts", {})
    hosts: list[HostConfig] = []
    for name, entry in hosts_block.items():
        target = entry.get("target") or entry.get("grpc_target")
        if not target:
            raise ValueError(f"host {name!r} missing 'target' field in marina config")
        hosts.append(HostConfig(name=name, target=target))

    seas_block = data.get("seas", {})
    seas: list[SeaConfig] = []
    for name, entry in seas_block.items():
        sea_type = entry.get("type")
        if not sea_type:
            raise ValueError(f"sea {name!r} missing 'type' field in marina config")
        seas.append(SeaConfig(name=name, type=sea_type, raw=dict(entry)))

    return MarinaConfig(hosts=hosts, seas=seas)
