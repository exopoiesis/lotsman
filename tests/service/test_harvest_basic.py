from __future__ import annotations

import tarfile
import time
from pathlib import Path

import grpc
import pytest

from lotsman.v1 import lotsman_pb2

pytestmark = pytest.mark.service


def _wait_done(stub, job_id: str, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = stub.Status(lotsman_pb2.StatusRequest(job_id=job_id))
        if status.state in (lotsman_pb2.DONE, lotsman_pb2.FAILED, lotsman_pb2.KILLED):
            return
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_harvest_inventory_and_archive_roundtrip(lotsman_grpc_stub):
    run = lotsman_grpc_stub.Run(
        lotsman_pb2.RunRequest(
            script="echo out-line\nprintf '{\"ok\": true}\\n' > result.json\n"
        )
    )
    _wait_done(lotsman_grpc_stub, run.job_id)

    inv = lotsman_grpc_stub.HarvestInventory(
        lotsman_pb2.HarvestInventoryRequest(job_id=run.job_id, mode="essential")
    )
    included_names = {Path(e.path).name for e in inv.entries if e.included}
    assert {"script.sh", "stdout.log", "stderr.log", "result.json"} <= included_names
    assert inv.included_bytes > 0

    harvest = lotsman_grpc_stub.Harvest(
        lotsman_pb2.HarvestRequest(job_id=run.job_id, mode="essential", format="tar.gz")
    )
    archive_path = Path(harvest.archive_path)
    assert archive_path.exists()
    assert harvest.archive_bytes == archive_path.stat().st_size
    assert harvest.sha256

    with tarfile.open(archive_path, "r:gz") as tf:
        assert {"script.sh", "stdout.log", "stderr.log", "result.json"} <= set(
            tf.getnames()
        )


def test_download_returns_bytes_and_supports_truncation(lotsman_grpc_stub, tmp_path: Path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"abcdef")

    full = lotsman_grpc_stub.Download(lotsman_pb2.DownloadRequest(path=str(target)))
    assert full.content == b"abcdef"
    assert full.total_bytes == 6
    assert full.truncated is False

    part = lotsman_grpc_stub.Download(
        lotsman_pb2.DownloadRequest(path=str(target), max_bytes=3)
    )
    assert part.content == b"abc"
    assert part.total_bytes == 6
    assert part.truncated is True


def test_download_glob_creates_guarded_archive(lotsman_grpc_stub, tmp_path: Path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "skip.bin").write_bytes(b"x")

    resp = lotsman_grpc_stub.DownloadGlob(
        lotsman_pb2.DownloadGlobRequest(
            pattern=str(tmp_path / "*.txt"),
            format="tar",
        )
    )
    assert Path(resp.archive_path).exists()
    assert {Path(e.path).name for e in resp.entries} == {"a.txt", "b.txt"}

    with tarfile.open(resp.archive_path, "r") as tf:
        assert set(tf.getnames()) == {"a.txt", "b.txt"}


def test_harvest_unknown_job_returns_not_found(lotsman_grpc_stub):
    with pytest.raises(grpc.RpcError) as exc:
        lotsman_grpc_stub.HarvestInventory(
            lotsman_pb2.HarvestInventoryRequest(
                job_id="local/UNKNOWN_NOTREAL",
                mode="essential",
            )
        )
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND
