from __future__ import annotations

import pytest

from lotsman.platform import runtime

pytestmark = pytest.mark.unit


def test_resolve_bash_prefers_git_bash_on_windows(monkeypatch):
    candidates = {
        r"C:\Program Files\Git\bin\bash.exe": r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Windows\System32\bash.exe": r"C:\Windows\System32\bash.exe",
        "bash": r"C:\Windows\System32\bash.exe",
    }

    monkeypatch.setattr(runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(runtime.Path, "exists", lambda self: str(self) in candidates)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: candidates.get(name))

    assert runtime.resolve_bash() == r"C:\Program Files\Git\bin\bash.exe"


def test_resolve_bash_raises_when_missing(monkeypatch):
    monkeypatch.setattr(runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError):
        runtime.resolve_bash()
