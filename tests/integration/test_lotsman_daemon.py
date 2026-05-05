from __future__ import annotations

import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import grpc
import pytest

from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_ready(proc: subprocess.Popen, timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        line = proc.stdout.readline() if proc.stdout else b""
        if b"serving on" in line.lower() or b"ready" in line.lower():
            return True
    return False


def test_lotsman_daemon_responds_to_whoami(tmp_path: Path):
    port = _free_port()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lotsman",
            "serve",
            "--port",
            str(port),
            "--host-id",
            "daemon-test",
            "--jobs-dir",
            str(jobs_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        assert _wait_ready(proc, timeout_s=5.0), "daemon did not become ready"

        channel = grpc.insecure_channel(f"localhost:{port}")
        try:
            stub = lotsman_pb2_grpc.LotsmanServiceStub(channel)
            resp = stub.Whoami(lotsman_pb2.WhoamiRequest(), timeout=3.0)
            assert resp.lotsman_version != ""
        finally:
            channel.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_lotsman_daemon_runs_a_job(tmp_path: Path):
    port = _free_port()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lotsman",
            "serve",
            "--port",
            str(port),
            "--host-id",
            "daemon-test",
            "--jobs-dir",
            str(jobs_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        assert _wait_ready(proc), "daemon did not become ready"

        channel = grpc.insecure_channel(f"localhost:{port}")
        try:
            stub = lotsman_pb2_grpc.LotsmanServiceStub(channel)
            r = stub.Run(lotsman_pb2.RunRequest(script="echo via-daemon\n"))
            assert r.job_id.startswith("daemon-test/")

            # Poll status to terminal
            deadline = time.time() + 5
            while time.time() < deadline:
                s = stub.Status(lotsman_pb2.StatusRequest(job_id=r.job_id))
                if s.state == lotsman_pb2.DONE:
                    break
                time.sleep(0.05)
            assert s.state == lotsman_pb2.DONE
            assert s.exit_code == 0
        finally:
            channel.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
