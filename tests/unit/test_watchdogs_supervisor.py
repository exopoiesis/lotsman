from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from lotsman.watchdogs.base import CheckResult, WatchdogContext
from lotsman.watchdogs.supervisor import Supervisor

pytestmark = pytest.mark.unit


@dataclass
class _StubCheck:
    name: str
    interval_sec: float = 0.0
    fire_after_calls: int = 1  # fires when call count reaches this
    raise_on_call: bool = False
    _calls: int = 0
    _result: CheckResult | None = None

    def check(self, ctx: WatchdogContext) -> CheckResult | None:
        self._calls += 1
        if self.raise_on_call:
            raise RuntimeError(f"{self.name} broken")
        if self._calls >= self.fire_after_calls:
            return CheckResult(name=self.name, detail=f"fired at call {self._calls}")
        return None

    @property
    def calls(self) -> int:
        return self._calls


def _ctx(job_id: str = "h/01TEST") -> WatchdogContext:
    return WatchdogContext(
        job_id=job_id,
        pid=1,
        started_at_unix_ms=0,
        last_activity_unix_ms=0,
        state="RUNNING",
        exit_code=None,
        job_dir=Path("/tmp/job"),
    )


@dataclass
class _ManualClock:
    t: float = 0.0

    def __call__(self) -> float:
        return self.t


def _provider(jobs: dict[str, WatchdogContext]) -> Callable[[str], WatchdogContext | None]:
    def _get(jid: str) -> WatchdogContext | None:
        return jobs.get(jid)

    return _get


# ---- registration ----


def test_register_then_history_empty() -> None:
    sup = Supervisor(_provider({}))
    sup.register("h/01", [_StubCheck("disk_low")])
    assert sup.history("h/01") == []
    assert [c.name for c in sup.list_watchdogs("h/01")] == ["disk_low"]


def test_unregister_removes_history() -> None:
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}))
    sup.register("h/01", [_StubCheck("x", interval_sec=0)])
    sup.tick()
    assert len(sup.history("h/01")) == 1
    sup.unregister("h/01")
    assert sup.history("h/01") == []


def test_double_register_is_idempotent() -> None:
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}))
    sup.register("h/01", [_StubCheck("a")])
    sup.register("h/01", [_StubCheck("b")])  # second call ignored
    assert [c.name for c in sup.list_watchdogs("h/01")] == ["a"]


# ---- tick ----


def test_tick_with_no_jobs_returns_empty() -> None:
    sup = Supervisor(_provider({}))
    assert sup.tick() == []


def test_tick_skips_when_ctx_none() -> None:
    sup = Supervisor(_provider({}))
    sup.register("h/01", [_StubCheck("a", interval_sec=0)])
    fired = sup.tick()
    assert fired == []
    # Job auto-unregistered when ctx is None.
    assert sup.list_watchdogs("h/01") == []


def test_tick_fires_check_and_records_event() -> None:
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}))
    sup.register("h/01", [_StubCheck("disk_low", interval_sec=0)])
    fired = sup.tick()
    assert len(fired) == 1
    job_id, result = fired[0]
    assert job_id == "h/01"
    assert result.name == "disk_low"
    assert sup.history("h/01") == [result]
    assert "disk_low" in sup.fired_names("h/01")


def test_tick_does_not_re_fire_same_check() -> None:
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}))
    chk = _StubCheck("disk_low", interval_sec=0)
    sup.register("h/01", [chk])
    sup.tick()
    sup.tick()
    sup.tick()
    assert len(sup.history("h/01")) == 1
    # check was called only once: idempotent firing skips even invocation
    assert chk.calls == 1


def test_tick_respects_interval_sec() -> None:
    clock = _ManualClock(t=100.0)
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}), clock=clock)
    chk = _StubCheck("disk_low", interval_sec=10.0, fire_after_calls=5)
    sup.register("h/01", [chk])

    sup.tick()  # 1st call (t=100)
    sup.tick()  # too soon (t=100), skipped
    sup.tick()  # too soon (t=100), skipped
    assert chk.calls == 1

    clock.t = 109.9
    sup.tick()  # still inside 10s window
    assert chk.calls == 1

    clock.t = 110.1
    sup.tick()  # past interval → check runs (call 2)
    clock.t = 120.2
    sup.tick()  # call 3
    clock.t = 130.3
    sup.tick()  # call 4
    clock.t = 140.4
    sup.tick()  # call 5 → fires
    assert chk.calls == 5
    assert len(sup.history("h/01")) == 1


def test_tick_handles_buggy_check_without_breaking_loop() -> None:
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}))
    bad = _StubCheck("bad", interval_sec=0, raise_on_call=True)
    good = _StubCheck("good", interval_sec=0)
    sup.register("h/01", [bad, good])
    fired = sup.tick()
    assert len(fired) == 1
    assert fired[0][1].name == "good"


def test_tick_processes_multiple_jobs() -> None:
    jobs = {"h/01": _ctx("h/01"), "h/02": _ctx("h/02")}
    sup = Supervisor(_provider(jobs))
    sup.register("h/01", [_StubCheck("a", interval_sec=0)])
    sup.register("h/02", [_StubCheck("b", interval_sec=0)])
    fired = sup.tick()
    job_ids = {jid for jid, _ in fired}
    assert job_ids == {"h/01", "h/02"}


# ---- listeners ----


def test_listener_invoked_on_fire() -> None:
    received: list[tuple[str, CheckResult]] = []
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}))
    sup.add_listener(lambda jid, r: received.append((jid, r)))
    sup.register("h/01", [_StubCheck("a", interval_sec=0)])
    sup.tick()
    assert len(received) == 1
    assert received[0][0] == "h/01"


def test_listener_exception_does_not_break_supervisor() -> None:
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}))
    sup.add_listener(lambda jid, r: (_ for _ in ()).throw(RuntimeError("boom")))
    captured: list[CheckResult] = []
    sup.add_listener(lambda jid, r: captured.append(r))
    sup.register("h/01", [_StubCheck("a", interval_sec=0)])
    sup.tick()
    assert len(captured) == 1


# ---- background thread ----


def test_start_and_stop_cleanly() -> None:
    sup = Supervisor(_provider({"h/01": _ctx("h/01")}))
    sup.register("h/01", [_StubCheck("a", interval_sec=0)])
    sup.start(period_sec=0.05)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if sup.history("h/01"):
            break
        time.sleep(0.02)
    sup.stop()
    assert len(sup.history("h/01")) == 1
