from __future__ import annotations

import hashlib
import os
from pathlib import Path

import grpc
import pytest

from lotsman.v1 import lotsman_pb2

pytestmark = pytest.mark.service


def test_upload_cat_stat_ls_roundtrip(lotsman_grpc_stub, tmp_path: Path):
    target = tmp_path / "workspace" / "inputs" / "run.in"
    content = b"&control\n  calculation='scf'\n/\n"

    upload = lotsman_grpc_stub.Upload(
        lotsman_pb2.UploadRequest(
            path=str(target),
            content=content,
            create_parents=True,
            overwrite=False,
        )
    )

    assert upload.path == str(target)
    assert upload.bytes_written == len(content)
    assert upload.sha256 == hashlib.sha256(content).hexdigest()
    assert target.read_bytes() == content

    stat = lotsman_grpc_stub.Stat(lotsman_pb2.StatRequest(path=str(target)))
    assert stat.exists is True
    assert stat.is_dir is False
    assert stat.size_bytes == len(content)

    cat = lotsman_grpc_stub.Cat(lotsman_pb2.CatRequest(path=str(target)))
    assert cat.content == content
    assert cat.total_bytes == len(content)
    assert cat.truncated is False

    listing = lotsman_grpc_stub.Ls(lotsman_pb2.LsRequest(path=str(target.parent)))
    assert [e.name for e in listing.entries] == ["run.in"]
    assert listing.entries[0].size_bytes == len(content)


def test_upload_refuses_overwrite_by_default(lotsman_grpc_stub, tmp_path: Path):
    target = tmp_path / "workspace" / "script.sh"
    target.parent.mkdir()
    target.write_bytes(b"old\n")

    with pytest.raises(grpc.RpcError) as exc:
        lotsman_grpc_stub.Upload(
            lotsman_pb2.UploadRequest(path=str(target), content=b"new\n")
        )

    assert exc.value.code() == grpc.StatusCode.ALREADY_EXISTS
    assert target.read_bytes() == b"old\n"


def test_upload_can_mark_file_executable(lotsman_grpc_stub, tmp_path: Path):
    target = tmp_path / "workspace" / "run.sh"

    lotsman_grpc_stub.Upload(
        lotsman_pb2.UploadRequest(
            path=str(target),
            content=b"#!/usr/bin/env bash\necho ok\n",
            create_parents=True,
            executable=True,
        )
    )

    if os.name == "posix":
        assert target.stat().st_mode & 0o111


def test_mkdir_and_disk_free(lotsman_grpc_stub, tmp_path: Path):
    target = tmp_path / "workspace" / "nested"

    mkdir = lotsman_grpc_stub.Mkdir(
        lotsman_pb2.MkdirRequest(path=str(target), parents=True, exist_ok=True)
    )
    assert mkdir.path == str(target)
    assert target.is_dir()

    disk = lotsman_grpc_stub.DiskFree(lotsman_pb2.DiskFreeRequest(path=str(target)))
    assert disk.total_bytes > 0
    assert disk.free_bytes > 0
    assert disk.used_bytes <= disk.total_bytes


def test_cat_unknown_file_returns_not_found(lotsman_grpc_stub, tmp_path: Path):
    with pytest.raises(grpc.RpcError) as exc:
        lotsman_grpc_stub.Cat(
            lotsman_pb2.CatRequest(path=str(tmp_path / "missing.txt"))
        )
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND
