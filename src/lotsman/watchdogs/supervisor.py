from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from lotsman.watchdogs.base import Check, CheckResult, WatchdogContext


@dataclass
class _JobSlot:
    job_id: str
    checks: list[Check]
    last_run_monotonic: dict[str, float] = field(default_factory=dict)
    fired: set[str] = field(default_factory=set)
    events: deque[CheckResult] = field(default_factory=lambda: deque(maxlen=500))


# Hooks invoked when a check fires. Used by Lotsman to fan-out to MCP clients.
EventListener = Callable[[str, CheckResult], None]


class Supervisor:
    """Per-Lotsman watchdog scheduler.

    Owns a set of registered jobs (jobId → list[Check]) and runs them on a
    background thread (`start()`) or synchronously (`tick()`). Jobs that
    have never been registered are silently ignored — the supervisor is
    state-driven, not event-driven, by design.

    A Check fires at most once per job (idempotent — first crossing wins).
    Re-firing on every tick would spam events without adding signal.
    """

    def __init__(
        self,
        ctx_provider: Callable[[str], WatchdogContext | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ctx_provider = ctx_provider
        self._clock = clock
        self._jobs: dict[str, _JobSlot] = {}
        self._listeners: list[EventListener] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- registration ----

    def register(self, job_id: str, checks: Iterable[Check]) -> None:
        with self._lock:
            if job_id in self._jobs:
                return
            self._jobs[job_id] = _JobSlot(job_id=job_id, checks=list(checks))

    def unregister(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    # ---- introspection ----

    def list_watchdogs(self, job_id: str) -> list[Check]:
        with self._lock:
            j = self._jobs.get(job_id)
            return list(j.checks) if j is not None else []

    def history(self, job_id: str) -> list[CheckResult]:
        with self._lock:
            j = self._jobs.get(job_id)
            return list(j.events) if j is not None else []

    def all_history(self) -> dict[str, list[CheckResult]]:
        with self._lock:
            return {jid: list(j.events) for jid, j in self._jobs.items()}

    def fired_names(self, job_id: str) -> set[str]:
        with self._lock:
            j = self._jobs.get(job_id)
            return set(j.fired) if j is not None else set()

    # ---- scheduling ----

    def tick(self) -> list[tuple[str, CheckResult]]:
        """Single sweep over all registered jobs. Returns events fired this tick."""
        fired: list[tuple[str, CheckResult]] = []
        now = self._clock()

        with self._lock:
            jobs_snapshot = list(self._jobs.values())

        for job in jobs_snapshot:
            ctx = self._ctx_provider(job.job_id)
            if ctx is None:
                # Job vanished — clean up.
                self.unregister(job.job_id)
                continue

            for check in job.checks:
                if check.name in job.fired:
                    continue
                last = job.last_run_monotonic.get(check.name, 0.0)
                if now - last < check.interval_sec:
                    continue
                job.last_run_monotonic[check.name] = now
                try:
                    result = check.check(ctx)
                except Exception:
                    # A buggy check must not break the supervisor loop.
                    continue
                if result is None:
                    continue
                job.fired.add(check.name)
                job.events.append(result)
                fired.append((job.job_id, result))

        # Fan-out outside the lock so listeners can re-enter.
        for job_id, result in fired:
            for listener in list(self._listeners):
                try:
                    listener(job_id, result)
                except Exception:
                    pass
        return fired

    def start(self, period_sec: float = 1.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    pass
                self._stop.wait(period_sec)

        self._thread = threading.Thread(target=_loop, name="watchdog-sup", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None
