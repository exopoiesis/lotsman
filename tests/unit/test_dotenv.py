from __future__ import annotations

import os
from pathlib import Path

import pytest

from marina.dotenv import find_env_files, load_dotenv, parse_env


def test_parse_env_basics() -> None:
    text = (
        "# a comment\n"
        "\n"
        "VAST_API_KEY=abc123\n"
        "export QUOTED='with spaces'\n"
        'DQUOTED="dq"\n'
        "  SPACED = trimmed \n"
        "no_equals_line\n"
        "=novalue_key\n"
    )
    parsed = parse_env(text)
    assert parsed["VAST_API_KEY"] == "abc123"
    assert parsed["QUOTED"] == "with spaces"  # export prefix + single quotes
    assert parsed["DQUOTED"] == "dq"
    assert parsed["SPACED"] == "trimmed"
    assert "no_equals_line" not in parsed
    assert "" not in parsed  # blank key skipped


def test_find_env_files_prefers_config_dir(tmp_path: Path) -> None:
    cfg = tmp_path / "sub" / "marina.toml"
    files = find_env_files(cfg)
    assert files[0] == cfg.parent / ".env"  # next to config first


def test_load_dotenv_sets_missing_but_never_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "marina.toml"
    (tmp_path / ".env").write_text(
        "VAST_API_KEY=fromfile\nALREADY_SET=fromfile\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VAST_API_KEY", raising=False)
    monkeypatch.setenv("ALREADY_SET", "fromenv")

    loaded = load_dotenv(cfg)

    assert os.environ["VAST_API_KEY"] == "fromfile"  # newly set
    assert os.environ["ALREADY_SET"] == "fromenv"  # existing wins
    assert "VAST_API_KEY" in loaded
    assert "ALREADY_SET" not in loaded
