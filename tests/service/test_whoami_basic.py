from __future__ import annotations

from concurrent import futures
from pathlib import Path

import grpc
import pytest

from lotsman.server import LotsmanService
from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc

pytestmark = pytest.mark.service


def _make_stub_with_manifest(manifest_path: Path | None, jobs_dir: Path):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = LotsmanService(host_id="local", jobs_dir=jobs_dir, manifest_path=manifest_path)
    lotsman_pb2_grpc.add_LotsmanServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    channel = grpc.insecure_channel(f"localhost:{port}")
    return server, servicer, channel, lotsman_pb2_grpc.LotsmanServiceStub(channel)


def test_whoami_without_manifest_returns_defaults(tmp_jobs_dir: Path):
    server, servicer, channel, stub = _make_stub_with_manifest(None, tmp_jobs_dir)
    try:
        resp = stub.Whoami(lotsman_pb2.WhoamiRequest())
        assert resp.tool == ""
        assert resp.image == ""
        assert resp.lotsman_version != ""  # always populated from package
    finally:
        channel.close()
        servicer.shutdown()
        server.stop(grace=None)


def test_whoami_returns_manifest_fields(tmp_jobs_dir: Path, tmp_path: Path):
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(
        """
[tool]
name = "qe"
version = "7.3"

[image]
name = "infra-qe-gpu"
tag = "server"

[defaults]
omp = 8
npool = 4
mpirun_required = true

pitfalls = ["mpirun mandatory on QE GPU"]
""",
        encoding="utf-8",
    )

    server, servicer, channel, stub = _make_stub_with_manifest(manifest, tmp_jobs_dir)
    try:
        resp = stub.Whoami(lotsman_pb2.WhoamiRequest())
        assert resp.tool == "qe"
        assert resp.tool_version == "7.3"
        assert resp.image == "infra-qe-gpu"
        assert resp.image_tag == "server"
        assert resp.default_omp == 8
        assert resp.default_npool == 4
        assert resp.mpirun_required is True
        assert "mpirun mandatory on QE GPU" in list(resp.known_pitfalls)
    finally:
        channel.close()
        servicer.shutdown()
        server.stop(grace=None)
