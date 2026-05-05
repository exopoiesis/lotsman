from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CheckResult:
    """A watchdog check fired.

    `severity` is informational for now (`notify`); `kill` and `checkpoint`
    are reserved for M3 when we wire the action-side. Until then, all fired
    results are simply recorded as events.
    """

    name: str
    detail: str
    severity: str = "notify"
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchdogContext:
    """Read-only view of a job's state passed to each `Check.check()`.

    Lotsman builds this snapshot on every supervisor tick from authoritative
    sources (job state machine, /proc, filesystem). Checks must not mutate.
    """

    job_id: str
    pid: int | None
    started_at_unix_ms: int
    last_activity_unix_ms: int
    state: str  # PENDING / RUNNING / DONE / FAILED / KILLED
    exit_code: int | None
    job_dir: Path  # path to <jobs_dir>/<ulid>/ for this specific job


class Check(Protocol):
    """A single watchdog rule. Implementations are dataclasses by convention.

    Checks are pure: given a context, decide whether a threshold was crossed
    and return either a CheckResult or None. Side effects (event delivery,
    notifications, killing) live in the supervisor and beyond.
    """

    name: str
    interval_sec: float

    def check(self, ctx: WatchdogContext) -> CheckResult | None: ...
