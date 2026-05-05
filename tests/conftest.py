from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures
from dataclasses import dataclass
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


@dataclass
class LotsmanHandle:
    target: str
    host_id: str
    servicer: LotsmanService
    server: grpc.Server


def _spawn_lotsman(host_id: str, jobs_dir: Path) -> LotsmanHandle:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = LotsmanService(host_id=host_id, jobs_dir=jobs_dir)
    lotsman_pb2_grpc.add_LotsmanServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    return LotsmanHandle(
        target=f"localhost:{port}",
        host_id=host_id,
        servicer=servicer,
        server=server,
    )


@pytest.fixture
def lotsman_tcp(tmp_path: Path) -> Iterator[LotsmanHandle]:
    jobs_dir = tmp_path / "lotsman1_jobs"
    jobs_dir.mkdir()
    handle = _spawn_lotsman("ws-1", jobs_dir)
    try:
        yield handle
    finally:
        handle.servicer.shutdown()
        handle.server.stop(grace=None)


@pytest.fixture
def two_lotsmen(tmp_path: Path) -> Iterator[tuple[LotsmanHandle, LotsmanHandle]]:
    h1 = _spawn_lotsman("ws-1", tmp_path / "lotsman1_jobs")
    (tmp_path / "lotsman1_jobs").mkdir(exist_ok=True)
    h2 = _spawn_lotsman("ws-2", tmp_path / "lotsman2_jobs")
    (tmp_path / "lotsman2_jobs").mkdir(exist_ok=True)
    try:
        yield h1, h2
    finally:
        h1.servicer.shutdown()
        h1.server.stop(grace=None)
        h2.servicer.shutdown()
        h2.server.stop(grace=None)
