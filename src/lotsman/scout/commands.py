from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv: list[str]
    status: str
    returncode: int | None
    duration_ms: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": self.argv,
            "status": self.status,
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def which_any(names: list[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def run_command(
    name: str,
    argv: list[str],
    *,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            env=dict(env) if env is not None else None,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResult(
            name=name,
            argv=argv,
            status="missing",
            returncode=None,
            duration_ms=duration_ms,
            stdout="",
            stderr=str(exc),
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResult(
            name=name,
            argv=argv,
            status="timeout",
            returncode=None,
            duration_ms=duration_ms,
            stdout=_text_or_empty(exc.stdout),
            stderr=_text_or_empty(exc.stderr),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    status = "ok" if completed.returncode == 0 else "failed"
    return CommandResult(
        name=name,
        argv=argv,
        status=status,
        returncode=completed.returncode,
        duration_ms=duration_ms,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _text_or_empty(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
