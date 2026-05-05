from __future__ import annotations

import time

import grpc
import pytest

from lotsman.v1 import lotsman_pb2

pytestmark = pytest.mark.service


def _wait_terminal(stub, job_id, timeout_s=5.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = stub.Status(lotsman_pb2.StatusRequest(job_id=job_id))
        if last.state in (lotsman_pb2.DONE, lotsman_pb2.FAILED, lotsman_pb2.KILLED):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached terminal state; last={last}")


def test_status_unknown_jobid_returns_not_found(lotsman_grpc_stub):
    with pytest.raises(grpc.RpcError) as exc:
        lotsman_grpc_stub.Status(lotsman_pb2.StatusRequest(job_id="local/UNKNOWN_NOT_REAL_42"))
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


def test_status_running_for_slow_job(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="sleep 1\n"))
    s = lotsman_grpc_stub.Status(lotsman_pb2.StatusRequest(job_id=r.job_id))
    assert s.state == lotsman_pb2.RUNNING
    assert s.started_at_unix_ms > 0
    assert not s.HasField("exit_code")


def test_status_eventually_reports_done(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="echo hello\n"))
    final = _wait_terminal(lotsman_grpc_stub, r.job_id)
    assert final.state == lotsman_pb2.DONE
    assert final.exit_code == 0
    assert final.started_at_unix_ms > 0
    assert final.finished_at_unix_ms >= final.started_at_unix_ms


def test_status_failed_job_reports_failed_with_exit_code(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="exit 7\n"))
    final = _wait_terminal(lotsman_grpc_stub, r.job_id)
    assert final.state == lotsman_pb2.FAILED
    assert final.exit_code == 7
