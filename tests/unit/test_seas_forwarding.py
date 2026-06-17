"""L1 tests for SSH port-forwarding helpers (no real ssh spawned)."""
from __future__ import annotations

import pytest

from marina.seas import forwarding
from marina.seas.forwarding import SubprocessForwarder, pick_free_port

pytestmark = pytest.mark.unit


def test_pick_free_port_returns_usable_port() -> None:
    port = pick_free_port()
    assert isinstance(port, int)
    assert 1024 < port < 65536


class _FakePopen:
    instances: list[list[str]] = []

    def __init__(self, argv: list[str], **_kw: object) -> None:
        _FakePopen.instances.append(argv)
        self.argv = argv
        self._terminated = False

    def poll(self) -> int | None:
        return None  # "still running"

    def terminate(self) -> None:
        self._terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover
        pass


def test_subprocess_forwarder_builds_ssh_forward_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakePopen.instances = []
    monkeypatch.setattr(forwarding.subprocess, "Popen", _FakePopen)

    fwd = SubprocessForwarder().open(
        ssh_host="ssh5.vast.ai",
        ssh_port=41022,
        remote_port=50051,
        ssh_user="root",
        identity_file="/keys/vast",
        local_port=55005,
    )
    assert fwd.local_port == 55005

    [argv] = _FakePopen.instances
    assert argv[0] == "ssh"
    assert "-N" in argv
    assert "-L" in argv
    assert "55005:127.0.0.1:50051" in argv
    assert "-p" in argv and "41022" in argv
    assert "-i" in argv and "/keys/vast" in argv
    assert argv[-1] == "root@ssh5.vast.ai"
    # never hang on host-key or password prompts
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "ExitOnForwardFailure=yes" in argv


def test_subprocess_forward_close_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(forwarding.subprocess, "Popen", _FakePopen)
    fwd = SubprocessForwarder().open(
        ssh_host="h", ssh_port=22, remote_port=50051, local_port=5000
    )
    fwd.close()  # should not raise
