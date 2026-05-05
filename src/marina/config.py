from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HostConfig:
    name: str
    target: str  # gRPC target, e.g., "localhost:50051" or "unix:///tmp/...sock"


@dataclass
class MarinaConfig:
    hosts: list[HostConfig] = field(default_factory=list)


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

    return MarinaConfig(hosts=hosts)
