from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Manifest:
    tool: str = ""
    tool_version: str = ""
    image: str = ""
    image_tag: str = ""
    default_omp: int = 0
    default_npool: int = 0
    mpirun_required: bool = False
    known_pitfalls: list[str] = field(default_factory=list)


def load_manifest(path: Path | None) -> Manifest:
    if path is None or not path.exists():
        return Manifest()

    with path.open("rb") as f:
        data = tomllib.load(f)

    tool = data.get("tool", {})
    image = data.get("image", {})
    defaults = data.get("defaults", {})

    pitfalls = defaults.get("pitfalls", data.get("pitfalls", []))

    return Manifest(
        tool=tool.get("name", ""),
        tool_version=tool.get("version", ""),
        image=image.get("name", ""),
        image_tag=image.get("tag", ""),
        default_omp=int(defaults.get("omp", 0)),
        default_npool=int(defaults.get("npool", 0)),
        mpirun_required=bool(defaults.get("mpirun_required", False)),
        known_pitfalls=list(pitfalls),
    )
