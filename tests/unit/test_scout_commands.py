from __future__ import annotations

import subprocess

import pytest

from lotsman.scout.commands import run_command

pytestmark = pytest.mark.unit


def test_run_command_reports_missing_binary():
    result = run_command(
        "missing",
        ["definitely-not-a-lotsman-scout-command"],
        timeout_s=1,
    )

    assert result.status == "missing"
    assert result.returncode is None


def test_run_command_reports_success(monkeypatch):
    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return subprocess.CompletedProcess(args=["tool"], returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("lotsman.scout.commands.subprocess.run", fake_run)
    result = run_command("tool", ["tool"], timeout_s=1)

    assert result.status == "ok"
    assert result.stdout == "ok"
