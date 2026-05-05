from __future__ import annotations

import time
from pathlib import Path

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


def test_run_returns_jobid_with_host_prefix(lotsman_grpc_stub):
    resp = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(script="echo hello\n", name="smoke")
    )
    assert resp.job_id.startswith("local/"), f"got {resp.job_id!r}"
    suffix = resp.job_id.split("/", 1)[1]
    assert len(suffix) == 26, f"ULID expected 26 chars, got {len(suffix)}: {suffix!r}"
    assert resp.state == lotsman_pb2.RUNNING


def test_run_strips_em_dash(lotsman_grpc_stub, tmp_jobs_dir: Path):
    resp = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(script="echo —flag value\n", name="emdash")
    )
    suffix = resp.job_id.split("/", 1)[1]
    saved = (tmp_jobs_dir / suffix / "script.sh").read_text(encoding="utf-8")
    assert "—" not in saved
    assert "--flag" in saved


def test_run_blocks_when_other_job_running(lotsman_grpc_stub):
    lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="sleep 30\n"))
    with pytest.raises(grpc.RpcError) as exc:
        lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="echo b\n"))
    assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_run_allowed_after_previous_done(lotsman_grpc_stub):
    r1 = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="echo a\n"))
    _wait_done(lotsman_grpc_stub, r1.job_id)

    r2 = lotsman_grpc_stub.Run(lotsman_pb2.RunRequest(script="echo b\n"))
    assert r2.job_id != r1.job_id
    assert r2.state == lotsman_pb2.RUNNING
