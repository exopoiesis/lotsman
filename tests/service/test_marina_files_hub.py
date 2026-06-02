from __future__ import annotations

from pathlib import Path

import pytest

from marina.hub import Hub

pytestmark = pytest.mark.service


def test_hub_upload_cat_stat_ls_roundtrip(lotsman_tcp, tmp_path: Path):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        target = tmp_path / "workspace" / "inputs" / "qe.in"
        content = b"&control\n/\n"

        uploaded = hub.upload(
            lotsman_tcp.host_id,
            path=str(target),
            content=content,
            create_parents=True,
        )
        assert uploaded.bytes_written == len(content)

        assert hub.cat(lotsman_tcp.host_id, str(target)).content == content
        assert hub.stat(lotsman_tcp.host_id, str(target)).size_bytes == len(content)
        assert [e.name for e in hub.ls(lotsman_tcp.host_id, str(target.parent)).entries] == [
            "qe.in"
        ]
    finally:
        hub.shutdown()


def test_hub_mkdir_and_disk_free(lotsman_tcp, tmp_path: Path):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        target = tmp_path / "workspace" / "runs"

        resp = hub.mkdir(lotsman_tcp.host_id, str(target), parents=True, exist_ok=True)
        assert resp.path == str(target)

        disk = hub.disk_free(lotsman_tcp.host_id, str(target))
        assert disk.free_bytes > 0
    finally:
        hub.shutdown()
