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


# ---- seas ----


def test_load_config_parses_seas(tmp_path: Path):
    p = tmp_path / "marina.toml"
    p.write_text(
        """
[seas.gomer]
type = "docker_sea"
docker_context = "gomer"
gpu_model = "RTX 4070"
gpu_count = 1
vram_gb = 12
fp64_native = false
cpu_ghz = 5.7
cpu_cores = 8
ram_gb = 32
disk_gb = 500
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert len(cfg.seas) == 1
    sea = cfg.seas[0]
    assert sea.name == "gomer"
    assert sea.type == "docker_sea"
    assert sea.raw["docker_context"] == "gomer"
    assert sea.raw["fp64_native"] is False
    assert sea.raw["vram_gb"] == 12


def test_load_config_seas_and_hosts_coexist(tmp_path: Path):
    p = tmp_path / "marina.toml"
    p.write_text(
        """
[hosts.legacy]
target = "localhost:55000"

[seas.gomer]
type = "docker_sea"
docker_context = "gomer"
gpu_model = "RTX 4070"
gpu_count = 1
vram_gb = 12
fp64_native = false
cpu_ghz = 5.7
cpu_cores = 8
ram_gb = 32
disk_gb = 500
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert [h.name for h in cfg.hosts] == ["legacy"]
    assert [s.name for s in cfg.seas] == ["gomer"]


def test_load_config_sea_missing_type_raises(tmp_path: Path):
    p = tmp_path / "marina.toml"
    p.write_text(
        """
[seas.broken]
docker_context = "gomer"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing 'type'"):
        load_config(p)


def test_load_config_no_seas_block(tmp_path: Path):
    p = tmp_path / "marina.toml"
    p.write_text(
        """
[hosts.w3]
target = "localhost:50051"
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.seas == []
