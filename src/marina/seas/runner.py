from __future__ import annotations

import subprocess
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
    """Real runner: spawns a child process, captures stdout/stderr."""
    completed = subprocess.run(  # noqa: S603 — argv is a list, not shell
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return RunResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
