"""SSH local port-forwarding for seas that reach Lotsman over SSH (Vast.ai, …).

DockerSea publishes the container's gRPC port to a host port and Marina dials
`127.0.0.1:<hostport>` directly. A rented cloud box has no such publish — the
only way in is SSH. So we open a long-lived `ssh -N -L <local>:127.0.0.1:<remote>`
tunnel and hand Marina `127.0.0.1:<local>` as the gRPC target.

The tunnel is a *persistent child process*, unlike the short blocking commands
that go through `Runner`. It gets its own injectable abstraction so VastSea can
be tested with a fake that never spawns ssh.
"""
from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from typing import Protocol


def pick_free_port() -> int:
    """Ask the OS for a free loopback TCP port, then release it.

    There's a small TOCTOU window before ssh binds it, but it's the standard
    trick and good enough for an ephemeral orchestration tunnel.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Forward(Protocol):
    """A live tunnel; `close()` tears it down."""

    @property
    def local_port(self) -> int: ...

    def close(self) -> None: ...


class Forwarder(Protocol):
    """Opens SSH local forwards. Injected into VastSea for testing."""

    def open(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        remote_port: int,
        ssh_user: str = "root",
        identity_file: str | None = None,
        local_port: int | None = None,
    ) -> Forward: ...


@dataclass
class _SubprocessForward:
    local_port: int
    _proc: subprocess.Popen[bytes]

    def close(self) -> None:
        if self._proc.poll() is not None:
            return  # already dead
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover — defensive
            self._proc.kill()


class SubprocessForwarder:
    """Real forwarder: spawns `ssh -N -L ...` as a detached child."""

    def open(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        remote_port: int,
        ssh_user: str = "root",
        identity_file: str | None = None,
        local_port: int | None = None,
    ) -> Forward:
        port = local_port if local_port is not None else pick_free_port()
        argv = [
            "ssh",
            "-N",  # no remote command, just forward
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-L", f"{port}:127.0.0.1:{remote_port}",
            "-p", str(ssh_port),
        ]
        if identity_file:
            argv += ["-i", identity_file]
        argv.append(f"{ssh_user}@{ssh_host}")
        proc = subprocess.Popen(  # noqa: S603 — argv is a list, not shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return _SubprocessForward(local_port=port, _proc=proc)
