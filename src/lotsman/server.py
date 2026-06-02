from __future__ import annotations

import hashlib
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn

import grpc
from ulid import ULID

from lotsman import __version__ as LOTSMAN_VERSION
from lotsman.jobs import TERMINAL_STATES, Job, now_ms
from lotsman.manifest import load_manifest
from lotsman.platform.logs import tail_bytes
from lotsman.platform.runtime import resolve_bash
from lotsman.platform.sanitize import sanitize_script
from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc
from lotsman.watchdogs import (
    Check,
    CheckResult,
    DiskLowCheck,
    GpuIdleCheck,
    ProcessExitOomCheck,
    Supervisor,
    WatchdogContext,
)

TAIL_POLL_INTERVAL_S = 0.05
WATCHDOG_TICK_S = 1.0
EVENT_QUEUE_POLL_S = 0.5

_STATE_NAMES: dict[int, str] = {
    lotsman_pb2.JOB_STATE_UNSPECIFIED: "UNSPECIFIED",
    lotsman_pb2.PENDING: "PENDING",
    lotsman_pb2.RUNNING: "RUNNING",
    lotsman_pb2.DONE: "DONE",
    lotsman_pb2.FAILED: "FAILED",
    lotsman_pb2.KILLED: "KILLED",
    lotsman_pb2.ORPHANED: "ORPHANED",
}


def _mtime_ms(path: Path) -> int:
    return int(path.stat().st_mtime * 1000)


def _entry_for(path: Path) -> lotsman_pb2.DirEntry:
    st = path.stat()
    return lotsman_pb2.DirEntry(
        name=path.name,
        path=str(path),
        is_dir=path.is_dir(),
        size_bytes=0 if path.is_dir() else st.st_size,
        mtime_unix_ms=int(st.st_mtime * 1000),
    )


def _abort(
    context: grpc.ServicerContext, code: grpc.StatusCode, message: str
) -> NoReturn:
    context.abort(code, message)
    raise RuntimeError(message)


def _default_checks_factory() -> list[Check]:
    """Production default watchdog set baked into every job.

    Light, host-portable defaults. Tool-specific watchdogs (scf_plateau,
    cons_qty_drift, h_anchor_violation, ...) belong in manifest.toml's
    `default_watchdogs` and arrive in M3.
    """
    return [DiskLowCheck(), ProcessExitOomCheck(), GpuIdleCheck()]


class _EventSubscription:
    """One open Events gRPC stream."""

    __slots__ = ("job_filter", "queue")

    def __init__(self, job_filter: str | None) -> None:
        self.job_filter = job_filter  # None = all jobs
        self.queue: queue.SimpleQueue[lotsman_pb2.Event] = queue.SimpleQueue()

    def push(self, event: lotsman_pb2.Event) -> None:
        if self.job_filter is None or event.job_id == self.job_filter:
            self.queue.put(event)


class LotsmanService(lotsman_pb2_grpc.LotsmanServiceServicer):
    def __init__(
        self,
        host_id: str = "local",
        jobs_dir: Path | None = None,
        manifest_path: Path | None = None,
        default_checks: list[Check] | None = None,
        default_checks_factory: Callable[[], list[Check]] | None = None,
    ) -> None:
        self.host_id = host_id
        self.jobs_dir = jobs_dir or Path("/var/lotsman/jobs")
        self.bash_path = resolve_bash()
        self.manifest = load_manifest(manifest_path)
        self.jobs: dict[str, Job] = {}

        # ---- watchdog state ----
        if default_checks is not None:
            self._default_checks_factory: Callable[[], list[Check]] = (
                lambda: list(default_checks)
            )
        elif default_checks_factory is not None:
            self._default_checks_factory = default_checks_factory
        else:
            self._default_checks_factory = _default_checks_factory

        self.supervisor = Supervisor(self._build_ctx)
        self._event_log: dict[str, list[lotsman_pb2.Event]] = {}
        self._subs: list[_EventSubscription] = []
        self._subs_lock = threading.Lock()
        self.supervisor.add_listener(self._on_fire)
        self.supervisor.start(period_sec=WATCHDOG_TICK_S)

    # ----- watchdog helpers -----

    def _build_ctx(self, job_id: str) -> WatchdogContext | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        job.poll_completion()
        return WatchdogContext(
            job_id=job_id,
            pid=job.proc.pid if job.proc is not None else None,
            started_at_unix_ms=job.started_at_ms or 0,
            last_activity_unix_ms=now_ms(),
            state=_STATE_NAMES.get(job.state, "UNSPECIFIED"),
            exit_code=job.exit_code,
            job_dir=job.job_dir,
        )

    def _on_fire(self, job_id: str, result: CheckResult) -> None:
        event = lotsman_pb2.Event(
            job_id=job_id,
            watchdog_name=result.name,
            event_type="watchdog_fired",
            unix_ms=now_ms(),
            detail=result.detail,
            severity=result.severity,
            data=dict(result.data),
        )
        self._event_log.setdefault(job_id, []).append(event)
        with self._subs_lock:
            subs = list(self._subs)
        for sub in subs:
            sub.push(event)

    # ----- job lifecycle helper -----

    def _running_job(self) -> Job | None:
        for job in self.jobs.values():
            if job.state == lotsman_pb2.RUNNING:
                job.poll_completion()
                if job.state == lotsman_pb2.RUNNING:
                    return job
        return None

    # ----- RPCs -----

    def Run(
        self,
        request: lotsman_pb2.RunRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.RunResponse:
        if (running := self._running_job()) is not None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"another job is already running: {running.job_id}",
            )

        ulid = str(ULID())
        job_id = f"{self.host_id}/{ulid}"

        job_dir = self.jobs_dir / ulid
        job_dir.mkdir(parents=True, exist_ok=True)
        script_path = job_dir / "script.sh"
        script_path.write_text(
            sanitize_script(request.script), encoding="utf-8", newline=""
        )

        stdout_h = (job_dir / "stdout.log").open("wb")
        stderr_h = (job_dir / "stderr.log").open("wb")
        proc = subprocess.Popen(
            [self.bash_path, str(script_path)],
            cwd=str(job_dir),
            stdout=stdout_h,
            stderr=stderr_h,
        )

        job = Job(
            job_id=job_id,
            state=lotsman_pb2.RUNNING,
            job_dir=job_dir,
            proc=proc,
            stdout_handle=stdout_h,
            stderr_handle=stderr_h,
            started_at_ms=now_ms(),
        )
        self.jobs[job_id] = job
        self.supervisor.register(job_id, self._default_checks_factory())

        return lotsman_pb2.RunResponse(job_id=job_id, state=lotsman_pb2.RUNNING)

    def Status(
        self,
        request: lotsman_pb2.StatusRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.StatusResponse:
        job = self.jobs.get(request.job_id)
        if job is None:
            _abort(context, grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

        job.poll_completion()

        resp = lotsman_pb2.StatusResponse(job_id=job.job_id, state=job.state)
        if job.exit_code is not None:
            resp.exit_code = job.exit_code
        if job.started_at_ms is not None:
            resp.started_at_unix_ms = job.started_at_ms
        if job.finished_at_ms is not None:
            resp.finished_at_unix_ms = job.finished_at_ms
        return resp

    def Kill(
        self,
        request: lotsman_pb2.KillRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.KillResponse:
        job = self.jobs.get(request.job_id)
        if job is None:
            _abort(context, grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

        grace_s = request.grace_sec if request.HasField("grace_sec") else 10.0
        force = request.force if request.HasField("force") else False

        job.poll_completion()
        was_running = job.state == lotsman_pb2.RUNNING
        if was_running:
            job.terminate(grace_s=grace_s, force=force)

        resp = lotsman_pb2.KillResponse(
            job_id=job.job_id,
            killed=was_running,
            state=job.state,
        )
        if job.exit_code is not None:
            resp.exit_code = job.exit_code
        return resp

    def Logs(
        self,
        request: lotsman_pb2.LogsRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.LogsResponse:
        job = self.jobs.get(request.job_id)
        if job is None:
            _abort(context, grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

        stdout_path = job.job_dir / "stdout.log"
        stderr_path = job.job_dir / "stderr.log"
        stdout_total = stdout_path.stat().st_size if stdout_path.exists() else 0
        stderr_total = stderr_path.stat().st_size if stderr_path.exists() else 0

        stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
        stderr_bytes = (
            stderr_path.read_bytes()
            if request.include_stderr and stderr_path.exists()
            else b""
        )

        if request.HasField("tail_lines"):
            stdout_bytes = tail_bytes(stdout_bytes, request.tail_lines)
            if request.include_stderr:
                stderr_bytes = tail_bytes(stderr_bytes, request.tail_lines)

        return lotsman_pb2.LogsResponse(
            job_id=job.job_id,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            stdout_total_bytes=stdout_total,
            stderr_total_bytes=stderr_total,
        )

    def TailFollow(
        self,
        request: lotsman_pb2.TailFollowRequest,
        context: grpc.ServicerContext,
    ) -> Iterator[lotsman_pb2.LogChunk]:
        job = self.jobs.get(request.job_id)
        if job is None:
            _abort(context, grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

        stdout_path = job.job_dir / "stdout.log"
        stderr_path = job.job_dir / "stderr.log"
        stdout_off = (
            request.from_offset_stdout if request.HasField("from_offset_stdout") else 0
        )
        stderr_off = (
            request.from_offset_stderr if request.HasField("from_offset_stderr") else 0
        )

        while context.is_active():
            new_stdout = b""
            new_stderr = b""

            if stdout_path.exists():
                size = stdout_path.stat().st_size
                if size > stdout_off:
                    with stdout_path.open("rb") as f:
                        f.seek(stdout_off)
                        new_stdout = f.read()
                    stdout_off = size

            if request.include_stderr and stderr_path.exists():
                size = stderr_path.stat().st_size
                if size > stderr_off:
                    with stderr_path.open("rb") as f:
                        f.seek(stderr_off)
                        new_stderr = f.read()
                    stderr_off = size

            job.poll_completion()
            is_terminal = job.state in TERMINAL_STATES

            if new_stdout or new_stderr or is_terminal:
                yield lotsman_pb2.LogChunk(
                    stdout=new_stdout,
                    stderr=new_stderr,
                    stdout_offset_after=stdout_off,
                    stderr_offset_after=stderr_off,
                    job_terminal=is_terminal,
                    state=job.state,
                )

            if is_terminal:
                return

            time.sleep(TAIL_POLL_INTERVAL_S)

    def Whoami(
        self,
        request: lotsman_pb2.WhoamiRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.WhoamiResponse:
        m = self.manifest
        return lotsman_pb2.WhoamiResponse(
            lotsman_version=LOTSMAN_VERSION,
            tool=m.tool,
            tool_version=m.tool_version,
            image=m.image,
            image_tag=m.image_tag,
            default_omp=m.default_omp,
            default_npool=m.default_npool,
            mpirun_required=m.mpirun_required,
            known_pitfalls=m.known_pitfalls,
        )

    def Events(
        self,
        request: lotsman_pb2.EventsRequest,
        context: grpc.ServicerContext,
    ) -> Iterator[lotsman_pb2.Event]:
        job_filter = request.job_id or None

        sub = _EventSubscription(job_filter=job_filter)

        # Replay history first if asked.
        if request.since_unix_ms > 0:
            for jid, events in self._event_log.items():
                if job_filter is not None and jid != job_filter:
                    continue
                for ev in events:
                    if ev.unix_ms >= request.since_unix_ms:
                        sub.queue.put(ev)

        with self._subs_lock:
            self._subs.append(sub)
        try:
            while context.is_active():
                try:
                    ev = sub.queue.get(timeout=EVENT_QUEUE_POLL_S)
                except queue.Empty:
                    continue
                yield ev
        finally:
            with self._subs_lock:
                if sub in self._subs:
                    self._subs.remove(sub)

    def WatchdogList(
        self,
        request: lotsman_pb2.WatchdogListRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.WatchdogListResponse:
        if request.job_id not in self.jobs:
            context.abort(grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

        checks = self.supervisor.list_watchdogs(request.job_id)
        fired = self.supervisor.fired_names(request.job_id)
        watchdogs = [
            lotsman_pb2.Watchdog(
                name=c.name,
                fired=c.name in fired,
                action="notify",
                interval_sec=float(c.interval_sec),
            )
            for c in checks
        ]
        return lotsman_pb2.WatchdogListResponse(
            job_id=request.job_id, watchdogs=watchdogs
        )

    def WatchdogHistory(
        self,
        request: lotsman_pb2.WatchdogHistoryRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.WatchdogHistoryResponse:
        if request.job_id not in self.jobs:
            context.abort(grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

        events = self._event_log.get(request.job_id, [])
        if request.since_unix_ms > 0:
            events = [e for e in events if e.unix_ms >= request.since_unix_ms]
        return lotsman_pb2.WatchdogHistoryResponse(
            job_id=request.job_id, events=list(events)
        )

    def EventsHistoryAll(
        self,
        request: lotsman_pb2.EventsHistoryAllRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.EventsHistoryAllResponse:
        all_events: list[lotsman_pb2.Event] = []
        since = request.since_unix_ms
        for events in self._event_log.values():
            for ev in events:
                if since > 0 and ev.unix_ms < since:
                    continue
                all_events.append(ev)
        all_events.sort(key=lambda e: e.unix_ms)
        return lotsman_pb2.EventsHistoryAllResponse(events=all_events)

    def Upload(
        self,
        request: lotsman_pb2.UploadRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.UploadResponse:
        path = Path(request.path)
        if not request.path:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "path is required")
        if path.exists() and not request.overwrite:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, f"path exists: {request.path!r}")
        if request.create_parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.parent.exists():
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"parent directory does not exist: {str(path.parent)!r}",
            )

        path.write_bytes(request.content)
        if request.executable:
            path.chmod(path.stat().st_mode | 0o111)
        return lotsman_pb2.UploadResponse(
            path=str(path),
            bytes_written=len(request.content),
            sha256=hashlib.sha256(request.content).hexdigest(),
        )

    def Mkdir(
        self,
        request: lotsman_pb2.MkdirRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.MkdirResponse:
        path = Path(request.path)
        if not request.path:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "path is required")
        try:
            path.mkdir(parents=request.parents, exist_ok=request.exist_ok)
        except FileExistsError:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, f"path exists: {request.path!r}")
        except FileNotFoundError:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"parent directory does not exist: {str(path.parent)!r}",
            )
        return lotsman_pb2.MkdirResponse(path=str(path))

    def Ls(
        self,
        request: lotsman_pb2.LsRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.LsResponse:
        path = Path(request.path)
        if not path.exists():
            context.abort(grpc.StatusCode.NOT_FOUND, f"path not found: {request.path!r}")
        if not path.is_dir():
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"path is not a directory: {request.path!r}",
            )
        entries = [_entry_for(p) for p in sorted(path.iterdir(), key=lambda p: p.name)]
        return lotsman_pb2.LsResponse(path=str(path), entries=entries)

    def Stat(
        self,
        request: lotsman_pb2.StatRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.StatResponse:
        path = Path(request.path)
        if not path.exists():
            return lotsman_pb2.StatResponse(path=str(path), exists=False)
        st = path.stat()
        return lotsman_pb2.StatResponse(
            path=str(path),
            exists=True,
            is_dir=path.is_dir(),
            size_bytes=0 if path.is_dir() else st.st_size,
            mtime_unix_ms=_mtime_ms(path),
        )

    def Cat(
        self,
        request: lotsman_pb2.CatRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.CatResponse:
        path = Path(request.path)
        if not path.exists():
            context.abort(grpc.StatusCode.NOT_FOUND, f"path not found: {request.path!r}")
        if path.is_dir():
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"path is a directory: {request.path!r}",
            )
        total = path.stat().st_size
        if request.HasField("max_bytes") and request.max_bytes < total:
            with path.open("rb") as f:
                content = f.read(request.max_bytes)
            truncated = True
        else:
            content = path.read_bytes()
            truncated = False
        return lotsman_pb2.CatResponse(
            path=str(path),
            content=content,
            total_bytes=total,
            truncated=truncated,
        )

    def DiskFree(
        self,
        request: lotsman_pb2.DiskFreeRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.DiskFreeResponse:
        path = Path(request.path or ".")
        target = path if path.exists() else path.parent
        if not target.exists():
            context.abort(grpc.StatusCode.NOT_FOUND, f"path not found: {request.path!r}")
        usage = shutil.disk_usage(target)
        return lotsman_pb2.DiskFreeResponse(
            path=str(path),
            total_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
        )

    def shutdown(self) -> None:
        self.supervisor.stop()
        for job in self.jobs.values():
            job.terminate()
