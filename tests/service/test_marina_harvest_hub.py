from __future__ import annotations

import time
from pathlib import Path

import pytest

from lotsman.v1 import lotsman_pb2
from marina.hub import Hub

pytestmark = pytest.mark.service


def _wait_done(hub: Hub, job_id: str, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = hub.status(job_id)
        if status.state in (lotsman_pb2.DONE, lotsman_pb2.FAILED, lotsman_pb2.KILLED):
            return
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_hub_harvest_inventory_harvest_and_download(lotsman_tcp):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        run = hub.run(lotsman_tcp.host_id, "echo hub-harvest\n")
        _wait_done(hub, run.job_id)

        inv = hub.harvest_inventory(run.job_id)
        assert inv.included_bytes > 0

        harvest = hub.harvest(run.job_id)
        assert harvest.archive_path

        downloaded = hub.download(lotsman_tcp.host_id, harvest.archive_path)
        assert downloaded.total_bytes == harvest.archive_bytes
        assert downloaded.content
    finally:
        hub.shutdown()


def test_hub_download_glob(lotsman_tcp, tmp_path: Path):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        (tmp_path / "one.log").write_text("1", encoding="utf-8")
        (tmp_path / "two.log").write_text("2", encoding="utf-8")

        resp = hub.download_glob(lotsman_tcp.host_id, str(tmp_path / "*.log"))
        assert {Path(e.path).name for e in resp.entries} == {"one.log", "two.log"}
        assert resp.archive_bytes > 0
    finally:
        hub.shutdown()
