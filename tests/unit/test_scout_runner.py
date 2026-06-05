from __future__ import annotations

import json
from pathlib import Path

import pytest

from lotsman.scout.commands import CommandResult
from lotsman.scout.runner import ScoutConfig, run_scout

pytestmark = pytest.mark.unit


def fake_runner(
    name: str,
    argv: list[str],
    timeout_s: float,
    env: dict[str, str] | None,
) -> CommandResult:
    del timeout_s, env
    stdout = ""
    if name.startswith("fio_"):
        stdout = json.dumps({"jobs": [{"jobname": name}]})
    return CommandResult(
        name=name,
        argv=argv,
        status="ok",
        returncode=0,
        duration_ms=1,
        stdout=stdout,
        stderr="",
    )


def test_scout_run_can_skip_external_probes(tmp_path: Path):
    result = run_scout(
        ScoutConfig(
            workspace=tmp_path,
            run_fio=False,
            run_stream=False,
            run_dmon=False,
            run_nvbandwidth=False,
            run_dcgm=False,
        ),
        command_runner=fake_runner,
    )

    assert result["schema_version"] == 1
    assert result["probes"] == {}
    assert "inventory" in result


def test_scout_fio_uses_workspace_and_cleans_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "lotsman.scout.runner.which_any",
        lambda names: names[0] if names == ["fio"] else None,
    )
    result = run_scout(
        ScoutConfig(
            workspace=tmp_path,
            fio_size_mb=1,
            fio_runtime_s=1,
            run_fio=True,
            run_stream=False,
            run_dmon=False,
            run_nvbandwidth=False,
            run_dcgm=False,
        ),
        command_runner=fake_runner,
    )

    probes = result["probes"]
    assert isinstance(probes, dict)
    fio = probes["fio"]
    assert isinstance(fio, dict)
    assert fio["status"] == "ok"
    assert not (tmp_path / "lotsman_scout_fio.bin").exists()
