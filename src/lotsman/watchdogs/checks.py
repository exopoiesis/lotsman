from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from lotsman.watchdogs.base import CheckResult, WatchdogContext


@dataclass
class DiskLowCheck:
    """Fire when free disk on the job's filesystem drops below `threshold_gb`.

    Default 5 GB matches the s126 lesson (DFT runs crashed on disk-full).
    """

    name: str = "disk_low"
    interval_sec: float = 60.0
    threshold_gb: float = 5.0
    # Test seam — replace shutil.disk_usage to simulate full disks.
    disk_usage: Callable[[str], shutil._ntuple_diskusage] = field(
        default=shutil.disk_usage
    )

    def check(self, ctx: WatchdogContext) -> CheckResult | None:
        try:
            usage = self.disk_usage(str(ctx.job_dir))
        except OSError:
            return None
        free_gb = usage.free / (1024**3)
        if free_gb >= self.threshold_gb:
            return None
        return CheckResult(
            name=self.name,
            detail=f"free={free_gb:.2f} GB < {self.threshold_gb} GB threshold",
            severity="notify",
            data={
                "free_gb": f"{free_gb:.2f}",
                "threshold_gb": f"{self.threshold_gb:.2f}",
                "job_dir": str(ctx.job_dir),
            },
        )


_OOM_EXIT_CODES = frozenset({9, 137})  # SIGKILL or 128+9


@dataclass
class ProcessExitOomCheck:
    """Fire when a job ended with an exit code typical of an OOM kill.

    Linux exits with 137 (128+SIGKILL) when the OOM killer reaps a process;
    a manual `kill -9` also produces 137/9. We can't distinguish here, so
    the event is informational ("likely OOM"). The watchdog is post-mortem
    by design — checks against a finished job's state.
    """

    name: str = "process_oom"
    interval_sec: float = 5.0

    def check(self, ctx: WatchdogContext) -> CheckResult | None:
        if ctx.state in ("PENDING", "RUNNING"):
            return None
        if ctx.exit_code is None:
            return None
        if ctx.exit_code not in _OOM_EXIT_CODES:
            return None
        return CheckResult(
            name=self.name,
            detail=f"job exited with code {ctx.exit_code} (likely OOM kill)",
            severity="notify",
            data={"exit_code": str(ctx.exit_code), "state": ctx.state},
        )


@dataclass
class GpuIdleCheck:
    """Fire when GPU utilization stays below `threshold_pct` for `idle_seconds`.

    Backed by `nvidia-smi --query-gpu=utilization.gpu`. If nvidia-smi is not
    on PATH (CPU host) the check is a no-op. The "stay below for N seconds"
    test uses an internal accumulator — supervisor calls it on each interval
    tick, GpuIdleCheck remembers when idleness started.

    Cost rationale: idle A100 = $0.70/hr drain. 30-min default catches
    overnight zombies before $14 vanishes.
    """

    name: str = "gpu_idle"
    interval_sec: float = 60.0
    threshold_pct: float = 5.0
    idle_seconds: float = 1800.0  # 30 min
    # Test seam — replace nvidia-smi shell out.
    sample_utilization: Callable[[], list[float] | None] | None = None

    _idle_start_unix_ms: int | None = None

    def check(self, ctx: WatchdogContext) -> CheckResult | None:
        if ctx.state != "RUNNING":
            self._idle_start_unix_ms = None
            return None

        sampler = self.sample_utilization or _default_nvidia_sampler
        utils = sampler()
        if utils is None:
            return None  # no GPU on this host
        # Use max across GPUs — if any GPU is busy, we're not idle.
        peak = max(utils) if utils else 0.0
        now_ms = ctx.last_activity_unix_ms

        if peak >= self.threshold_pct:
            self._idle_start_unix_ms = None
            return None

        if self._idle_start_unix_ms is None:
            self._idle_start_unix_ms = now_ms
            return None

        idle_ms = now_ms - self._idle_start_unix_ms
        if idle_ms < self.idle_seconds * 1000:
            return None

        return CheckResult(
            name=self.name,
            detail=(
                f"GPU peak utilization {peak:.1f}% < {self.threshold_pct}% "
                f"for {idle_ms / 1000:.0f}s (>= {self.idle_seconds:.0f}s)"
            ),
            severity="notify",
            data={
                "peak_pct": f"{peak:.1f}",
                "idle_seconds": f"{idle_ms / 1000:.0f}",
                "threshold_pct": f"{self.threshold_pct:.1f}",
            },
        )


def _default_nvidia_sampler() -> list[float] | None:
    """Run `nvidia-smi` and parse utilization for each GPU. None if unavailable."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    utils = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            utils.append(float(line))
        except ValueError:
            continue
    return utils
