from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures
from pathlib import Path

import grpc
import pytest

from lotsman.server import LotsmanService
from lotsman.v1 import lotsman_pb2_grpc


@pytest.fixture
def tmp_jobs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "jobs"
    d.mkdir()
    return d


@pytest.fixture
def lotsman_grpc_stub(tmp_jobs_dir: Path) -> Iterator[lotsman_pb2_grpc.LotsmanServiceStub]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = LotsmanService(host_id="local", jobs_dir=tmp_jobs_dir)
    lotsman_pb2_grpc.add_LotsmanServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    server.start()

    channel = grpc.insecure_channel(f"localhost:{port}")
    stub = lotsman_pb2_grpc.LotsmanServiceStub(channel)
    try:
        yield stub
    finally:
        channel.close()
        servicer.shutdown()
        server.stop(grace=None)
