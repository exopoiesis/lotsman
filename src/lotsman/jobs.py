from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from lotsman.v1 import lotsman_pb2

TERMINAL_STATES = (lotsman_pb2.DONE, lotsman_pb2.FAILED, lotsman_pb2.KILLED)


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Job:
    job_id: str
    state: int  # lotsman_pb2.JobState value
    job_dir: Path
    proc: subprocess.Popen | None = None
    stdout_handle: IO[bytes] | None = None
    stderr_handle: IO[bytes] | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    exit_code: int | None = None

    def poll_completion(self) -> None:
        if self.state != lotsman_pb2.RUNNING or self.proc is None:
            return
        rc = self.proc.poll()
        if rc is None:
            return
        self.exit_code = rc
        self.state = lotsman_pb2.DONE if rc == 0 else lotsman_pb2.FAILED
        self.finished_at_ms = now_ms()
        self._close_handles()

    def terminate(self, grace_s: float = 2.0, force: bool = False) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            self._close_handles()
            return
        if force:
            self.proc.kill()
        else:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.wait()
        self.exit_code = self.proc.returncode
        self.state = lotsman_pb2.KILLED
        self.finished_at_ms = now_ms()
        self._close_handles()

    def _close_handles(self) -> None:
        for h in (self.stdout_handle, self.stderr_handle):
            if h is not None and not h.closed:
                h.close()
        self.stdout_handle = None
        self.stderr_handle = None
