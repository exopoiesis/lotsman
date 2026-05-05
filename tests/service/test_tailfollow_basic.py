from __future__ import annotations

import grpc
import pytest

from lotsman.v1 import lotsman_pb2

pytestmark = pytest.mark.service


def _consume(stream, max_chunks=100):
    chunks = []
    for chunk in stream:
        chunks.append(chunk)
        if chunk.job_terminal or len(chunks) >= max_chunks:
            break
    return chunks


def test_tailfollow_unknown_jobid_returns_not_found(lotsman_grpc_stub):
    stream = lotsman_grpc_stub.TailFollow(
        lotsman_pb2.TailFollowRequest(job_id="local/UNKNOWN_NOTREAL_42")
    )
    with pytest.raises(grpc.RpcError) as exc:
        next(stream)
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


def test_tailfollow_streams_until_done(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(script="echo first\necho second\necho third\n")
    )
    chunks = _consume(lotsman_grpc_stub.TailFollow(lotsman_pb2.TailFollowRequest(job_id=r.job_id)))

    assert chunks, "expected at least one chunk"
    assert chunks[-1].job_terminal is True
    assert chunks[-1].state == lotsman_pb2.DONE

    full_stdout = b"".join(c.stdout for c in chunks)
    assert full_stdout == b"first\nsecond\nthird\n"


def test_tailfollow_resumes_from_offset(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(script="echo aaa\necho bbb\necho ccc\n")
    )
    # Drain to completion first
    _consume(lotsman_grpc_stub.TailFollow(lotsman_pb2.TailFollowRequest(job_id=r.job_id)))

    # Now request from offset 4 (skip "aaa\n")
    chunks = _consume(
        lotsman_grpc_stub.TailFollow(
            lotsman_pb2.TailFollowRequest(job_id=r.job_id, from_offset_stdout=4)
        )
    )
    full_stdout = b"".join(c.stdout for c in chunks)
    assert full_stdout == b"bbb\nccc\n"
    assert chunks[-1].job_terminal is True


def test_tailfollow_includes_stderr_when_requested(lotsman_grpc_stub):
    r = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(script="echo to_out\necho to_err >&2\n")
    )
    chunks = _consume(
        lotsman_grpc_stub.TailFollow(
            lotsman_pb2.TailFollowRequest(job_id=r.job_id, include_stderr=True)
        )
    )
    full_stdout = b"".join(c.stdout for c in chunks)
    full_stderr = b"".join(c.stderr for c in chunks)
    assert full_stdout == b"to_out\n"
    assert full_stderr == b"to_err\n"
