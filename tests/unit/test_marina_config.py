from __future__ import annotations

from pathlib import Path

import pytest

from marina.config import load_config

pytestmark = pytest.mark.unit


def test_load_config_missing_path_empty():
    cfg = load_config(None)
    assert cfg.hosts == []


def test_load_config_nonexistent_file_empty(tmp_path: Path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.hosts == []


def test_load_config_parses_hosts(tmp_path: Path):
    p = tmp_path / "marina.toml"
    p.write_text(
        """
[hosts.w3]
target = "localhost:50051"

[hosts.gomer]
target = "localhost:50052"
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert len(cfg.hosts) == 2
    by_name = {h.name: h.target for h in cfg.hosts}
    assert by_name["w3"] == "localhost:50051"
    assert by_name["gomer"] == "localhost:50052"


def test_load_config_missing_target_raises(tmp_path: Path):
    p = tmp_path / "marina.toml"
    p.write_text(
        """
[hosts.broken]
notes = "no target field"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing 'target'"):
        load_config(p)


def test_load_config_grpc_target_alias(tmp_path: Path):
    p = tmp_path / "marina.toml"
    p.write_text(
        """
[hosts.alt]
grpc_target = "unix:///tmp/lotsman.sock"
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.hosts[0].target == "unix:///tmp/lotsman.sock"
