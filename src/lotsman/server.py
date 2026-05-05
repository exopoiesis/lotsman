from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import grpc
from ulid import ULID

from lotsman import __version__ as LOTSMAN_VERSION
from lotsman.jobs import TERMINAL_STATES, Job, now_ms
from lotsman.manifest import load_manifest
from lotsman.platform.logs import tail_bytes
from lotsman.platform.runtime import resolve_bash
from lotsman.platform.sanitize import sanitize_script
from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc

TAIL_POLL_INTERVAL_S = 0.05


class LotsmanService(lotsman_pb2_grpc.LotsmanServiceServicer):
    def __init__(
        self,
        host_id: str = "local",
        jobs_dir: Path | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self.host_id = host_id
        self.jobs_dir = jobs_dir or Path("/var/lotsman/jobs")
        self.bash_path = resolve_bash()
        self.manifest = load_manifest(manifest_path)
        self.jobs: dict[str, Job] = {}

    def _running_job(self) -> Job | None:
        for job in self.jobs.values():
            if job.state == lotsman_pb2.RUNNING:
                job.poll_completion()
                if job.state == lotsman_pb2.RUNNING:
                    return job
        return None

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

        return lotsman_pb2.RunResponse(job_id=job_id, state=lotsman_pb2.RUNNING)

    def Status(
        self,
        request: lotsman_pb2.StatusRequest,
        context: grpc.ServicerContext,
    ) -> lotsman_pb2.StatusResponse:
        job = self.jobs.get(request.job_id)
        if job is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

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
            context.abort(grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

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
            context.abort(grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

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
            context.abort(grpc.StatusCode.NOT_FOUND, f"unknown job {request.job_id!r}")

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

    def shutdown(self) -> None:
        for job in self.jobs.values():
            job.terminate()
