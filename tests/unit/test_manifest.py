from __future__ import annotations

from pathlib import Path

import pytest

from lotsman.manifest import load_manifest

pytestmark = pytest.mark.unit


def test_load_manifest_missing_path_returns_empty():
    m = load_manifest(None)
    assert m.tool == ""
    assert m.image == ""
    assert m.known_pitfalls == []


def test_load_manifest_nonexistent_file_returns_empty(tmp_path: Path):
    m = load_manifest(tmp_path / "missing.toml")
    assert m.tool == ""


def test_load_manifest_parses_qe_example(tmp_path: Path):
    p = tmp_path / "manifest.toml"
    p.write_text(
        """
[tool]
name = "qe"
version = "7.3"

[image]
name = "infra-qe-gpu"
tag = "server"

[defaults]
omp = 8
npool = 4
mpirun_required = true

pitfalls = [
  "QE GPU silent crash without mpirun",
  "ENVIRON only for slabs/molecules, not bulk 3D",
]
""",
        encoding="utf-8",
    )

    m = load_manifest(p)
    assert m.tool == "qe"
    assert m.tool_version == "7.3"
    assert m.image == "infra-qe-gpu"
    assert m.image_tag == "server"
    assert m.default_omp == 8
    assert m.default_npool == 4
    assert m.mpirun_required is True
    assert "QE GPU silent crash without mpirun" in m.known_pitfalls
    assert len(m.known_pitfalls) == 2


def test_load_manifest_partial_fields_defaults_zero(tmp_path: Path):
    p = tmp_path / "manifest.toml"
    p.write_text("[tool]\nname = \"cp2k\"\n", encoding="utf-8")
    m = load_manifest(p)
    assert m.tool == "cp2k"
    assert m.tool_version == ""
    assert m.default_omp == 0
    assert m.mpirun_required is False
