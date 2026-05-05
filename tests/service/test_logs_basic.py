from __future__ import annotations

import time

import grpc
import pytest

from lotsman.v1 import lotsman_pb2

pytestmark = pytest.mark.service


def _wait_done(stub, job_id, timeout_s=5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        s = stub.Status(lotsman_pb2.StatusRequest(job_id=job_id))
        if s.state in (lotsman_pb2.DONE, lotsman_pb2.FAILED, lotsman_pb2.KILLED):
            return s
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_logs_unknown_jobid_returns_not_found(lotsman_grpc_stub):
    with pytest.raises(grpc.RpcError) as exc:
        lotsman_grpc_stub.Logs(lotsman_pb2.LogsRequest(job_id="local/UNKNOWN_NOTREAL"))
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


def test_logs_returns_stdout_for_done_job(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="echo hello world\n"))
    _wait_done(lotsman_grpc_stub, r.job_id)

    resp = lotsman_grpc_stub.Logs(lotsman_pb2.LogsRequest(job_id=r.job_id))
    assert resp.job_id == r.job_id
    assert resp.stdout == b"hello world\n"
    assert resp.stderr == b""  # not included by default
    assert resp.stdout_total_bytes == len(b"hello world\n")


def test_logs_includes_stderr_when_requested(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(script="echo on_out\necho on_err >&2\n")
    )
    _wait_done(lotsman_grpc_stub, r.job_id)

    resp = lotsman_grpc_stub.Logs(
        lotsman_pb2.LogsRequest(job_id=r.job_id, include_stderr=True)
    )
    assert resp.stdout == b"on_out\n"
    assert resp.stderr == b"on_err\n"


def test_logs_tail_lines_returns_last_n(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(script="for i in 1 2 3 4 5; do echo line_$i; done\n")
    )
    _wait_done(lotsman_grpc_stub, r.job_id)

    resp = lotsman_grpc_stub.Logs(
        lotsman_pb2.LogsRequest(job_id=r.job_id, tail_lines=2)
    )
    assert resp.stdout == b"line_4\nline_5\n"
    # but stdout_total_bytes reports full file size
    assert resp.stdout_total_bytes > len(resp.stdout)


def test_logs_works_on_running_job(lotsman_grpc_stub):
    # Long-running job that emits early then sleeps
    r = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(script="echo first_line\nsleep 30\n")
    )

    # Wait briefly for first echo to flush
    deadline = time.time() + 2.0
    while time.time() < deadline:
        resp = lotsman_grpc_stub.Logs(lotsman_pb2.LogsRequest(job_id=r.job_id))
        if resp.stdout:
            break
        time.sleep(0.05)

    assert resp.stdout == b"first_line\n"
    s = lotsman_grpc_stub.Status(lotsman_pb2.StatusRequest(job_id=r.job_id))
    assert s.state == lotsman_pb2.RUNNING
