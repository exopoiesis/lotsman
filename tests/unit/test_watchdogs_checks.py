from __future__ import annotations

import os
from pathlib import Path

import pytest

from lotsman.watchdogs.base import WatchdogContext
from lotsman.watchdogs.checks import (
    DiskLowCheck,
    GpuIdleCheck,
    ProcessExitOomCheck,
)

pytestmark = pytest.mark.unit


def _ctx(
    *,
    state: str = "RUNNING",
    exit_code: int | None = None,
    last_activity_unix_ms: int = 0,
    job_dir: Path | None = None,
) -> WatchdogContext:
    return WatchdogContext(
        job_id="local/01HPTEST",
        pid=12345,
        started_at_unix_ms=0,
        last_activity_unix_ms=last_activity_unix_ms,
        state=state,
        exit_code=exit_code,
        job_dir=job_dir or Path("/tmp/job"),
    )


# ---- DiskLowCheck ----


def _fake_disk_usage(free_bytes: int):
    """Return a stub that mimics shutil._ntuple_diskusage."""

    def _stub(path: str):
        return os.statvfs_result if False else _DiskUsage(  # type: ignore[truthy-bool]
            total=10 * 1024**3, used=10 * 1024**3 - free_bytes, free=free_bytes
        )

    return _stub


class _DiskUsage:
    __slots__ = ("total", "used", "free")

    def __init__(self, total: int, used: int, free: int) -> None:
        self.total = total
        self.used = used
        self.free = free


def test_disk_low_no_fire_when_free_above_threshold() -> None:
    chk = DiskLowCheck(threshold_gb=5.0, disk_usage=_fake_disk_usage(10 * 1024**3))
    assert chk.check(_ctx()) is None


def test_disk_low_fires_when_free_below_threshold() -> None:
    chk = DiskLowCheck(threshold_gb=5.0, disk_usage=_fake_disk_usage(2 * 1024**3))
    result = chk.check(_ctx())
    assert result is not None
    assert result.name == "disk_low"
    assert "free=2.00 GB" in result.detail
    assert result.data["threshold_gb"] == "5.00"


def test_disk_low_swallows_oserror() -> None:
    def boom(path: str) -> _DiskUsage:
        raise OSError("disk gone")

    chk = DiskLowCheck(threshold_gb=5.0, disk_usage=boom)  # type: ignore[arg-type]
    assert chk.check(_ctx()) is None


# ---- ProcessExitOomCheck ----


def test_process_oom_no_fire_while_running() -> None:
    chk = ProcessExitOomCheck()
    assert chk.check(_ctx(state="RUNNING")) is None


def test_process_oom_no_fire_when_no_exit_code() -> None:
    chk = ProcessExitOomCheck()
    assert chk.check(_ctx(state="DONE", exit_code=None)) is None


def test_process_oom_no_fire_on_normal_exit() -> None:
    chk = ProcessExitOomCheck()
    assert chk.check(_ctx(state="DONE", exit_code=0)) is None
    assert chk.check(_ctx(state="DONE", exit_code=1)) is None


@pytest.mark.parametrize("code", [9, 137])
def test_process_oom_fires_on_kill_codes(code: int) -> None:
    chk = ProcessExitOomCheck()
    result = chk.check(_ctx(state="FAILED", exit_code=code))
    assert result is not None
    assert result.name == "process_oom"
    assert str(code) in result.detail
    assert result.data["exit_code"] == str(code)


# ---- GpuIdleCheck ----


def test_gpu_idle_no_fire_when_no_nvidia_smi() -> None:
    chk = GpuIdleCheck(sample_utilization=lambda: None)
    assert chk.check(_ctx(state="RUNNING")) is None


def test_gpu_idle_no_fire_when_gpu_busy() -> None:
    chk = GpuIdleCheck(threshold_pct=5.0, sample_utilization=lambda: [42.0])
    assert chk.check(_ctx(state="RUNNING", last_activity_unix_ms=0)) is None


def test_gpu_idle_starts_clock_on_first_idle_sample() -> None:
    chk = GpuIdleCheck(
        threshold_pct=5.0,
        idle_seconds=60.0,
        sample_utilization=lambda: [1.0],
    )
    # First idle sample arms the clock — does NOT fire yet.
    result = chk.check(_ctx(state="RUNNING", last_activity_unix_ms=1_000_000))
    assert result is None
    # Same wall time → still no fire (no elapsed idle window).
    result = chk.check(_ctx(state="RUNNING", last_activity_unix_ms=1_000_000))
    assert result is None


def test_gpu_idle_fires_after_idle_window_elapses() -> None:
    chk = GpuIdleCheck(
        threshold_pct=5.0,
        idle_seconds=60.0,
        sample_utilization=lambda: [1.0],
    )
    chk.check(_ctx(state="RUNNING", last_activity_unix_ms=1_000_000))
    # 70 s later → past 60 s threshold, fire.
    result = chk.check(_ctx(state="RUNNING", last_activity_unix_ms=1_070_000))
    assert result is not None
    assert result.name == "gpu_idle"
    assert "GPU peak utilization 1.0%" in result.detail
    assert result.data["peak_pct"] == "1.0"


def test_gpu_idle_resets_clock_when_gpu_becomes_busy() -> None:
    state = {"util": [1.0]}
    chk = GpuIdleCheck(
        threshold_pct=5.0,
        idle_seconds=60.0,
        sample_utilization=lambda: state["util"],
    )
    chk.check(_ctx(state="RUNNING", last_activity_unix_ms=0))
    state["util"] = [80.0]
    chk.check(_ctx(state="RUNNING", last_activity_unix_ms=30_000))
    state["util"] = [1.0]
    # Clock has been reset; needs another full idle_seconds to fire.
    result = chk.check(_ctx(state="RUNNING", last_activity_unix_ms=60_000))
    assert result is None
    result = chk.check(_ctx(state="RUNNING", last_activity_unix_ms=130_000))
    assert result is not None


def test_gpu_idle_resets_clock_when_job_not_running() -> None:
    chk = GpuIdleCheck(
        threshold_pct=5.0,
        idle_seconds=60.0,
        sample_utilization=lambda: [1.0],
    )
    chk.check(_ctx(state="RUNNING", last_activity_unix_ms=0))
    # Job moved to DONE → clock resets.
    chk.check(_ctx(state="DONE", last_activity_unix_ms=70_000))
    # Back to RUNNING with same idle GPU; clock starts over.
    result = chk.check(_ctx(state="RUNNING", last_activity_unix_ms=80_000))
    assert result is None
