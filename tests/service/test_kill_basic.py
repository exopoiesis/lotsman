from __future__ import annotations

import time

import grpc
import pytest

from lotsman.v1 import lotsman_pb2

pytestmark = pytest.mark.service


def test_kill_unknown_jobid_returns_not_found(lotsman_grpc_stub):
    with pytest.raises(grpc.RpcError) as exc:
        lotsman_grpc_stub.Kill(lotsman_pb2.KillRequest(job_id="local/UNKNOWN_42"))
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


def test_kill_running_job_transitions_to_killed(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="sleep 30\n"))
    resp = lotsman_grpc_stub.Kill(lotsman_pb2.KillRequest(job_id=r.job_id, grace_sec=1.0))
    assert resp.killed is True
    assert resp.state == lotsman_pb2.KILLED

    # Status confirms terminal state
    s = lotsman_grpc_stub.Status(lotsman_pb2.StatusRequest(job_id=r.job_id))
    assert s.state == lotsman_pb2.KILLED


def test_kill_already_done_job_is_idempotent(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="echo hi\n"))
    deadline = time.time() + 5.0
    while time.time() < deadline:
        s = lotsman_grpc_stub.Status(lotsman_pb2.StatusRequest(job_id=r.job_id))
        if s.state == lotsman_pb2.DONE:
            break
        time.sleep(0.02)
    assert s.state == lotsman_pb2.DONE

    resp = lotsman_grpc_stub.Kill(lotsman_pb2.KillRequest(job_id=r.job_id))
    assert resp.killed is False
    assert resp.state == lotsman_pb2.DONE
    assert resp.exit_code == 0


def test_kill_unlocks_run(lotsman_grpc_stub):
    r1 = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="sleep 30\n"))
    lotsman_grpc_stub.Kill(lotsman_pb2.KillRequest(job_id=r1.job_id, grace_sec=1.0))

    # Now Run is allowed again
    r2 = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="echo b\n"))
    assert r2.job_id != r1.job_id
    assert r2.state == lotsman_pb2.RUNNING
