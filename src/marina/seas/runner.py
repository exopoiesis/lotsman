from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    """Run an argv list and return its result.

    Injected into DockerSea so unit/service tests can replace `docker` with
    a scripted fake without monkey-patching subprocess.
    """

    def __call__(self, argv: list[str], *, timeout: float | None = None) -> RunResult: ...


def subprocess_runner(argv: list[str], *, timeout: float | None = None) -> RunResult:
    """Real runner: spawns a child process, collecting stdout/stderr via files.

    Two deliberate choices make this safe to call from inside the Marina MCP
    server (whose own stdio is the JSON-RPC pipe to the client):

    - ``stdin`` is DEVNULL, so a child that reads stdin (e.g. a CLI's first-run
      prompt) gets EOF instead of blocking forever on a pipe that never feeds.
    - stdout/stderr go to temp **files**, not pipes. With pipes,
      ``subprocess.run`` waits for EOF on the read end, which a lingering
      grandchild that inherited the write handle can hold open indefinitely —
      hanging the server even after the command itself finished (seen with the
      ``vastai`` console-script shim spawning a python child on Windows). With
      files we wait only for the process to exit. None of our commands feed
      stdin or need streaming output.
    """
    with (
        tempfile.TemporaryFile() as out,
        tempfile.TemporaryFile() as err,
    ):
        completed = subprocess.run(  # noqa: S603 — argv is a list, not shell
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            timeout=timeout,
        )
        out.seek(0)
        err.seek(0)
        return RunResult(
            returncode=completed.returncode,
            stdout=out.read().decode("utf-8", "replace"),
            stderr=err.read().decode("utf-8", "replace"),
        )
