"""L3 integration: DockerSea against a real local Docker daemon.

Skipped automatically if `docker info` is not reachable. Builds
`lotsman:test-l3` once per session from the repo Dockerfile, then exercises
the full Marina → DockerSea → real container → Lotsman gRPC flow:

    host_create  →  docker run -d --label lotsman_managed=1 …
    Whoami       →  real gRPC into the new container
    Run echo     →  Lotsman spawns a bash subprocess
    Status/Logs  →  poll until DONE, read stdout
    host_destroy →  docker rm -f

This is the "the abstraction actually works end-to-end" gate. If anything
real about the Sea ↔ Hub ↔ Lotsman boundary is broken, this fails.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import grpc
import pytest

from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc
from marina.hub import Hub
from marina.seas.docker_sea import DockerSea, DockerSeaCapability

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE = "lotsman:test-l3"
DEFAULT_CONTEXT = "default"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "--context", DEFAULT_CONTEXT, "info"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_available(),
        reason="docker daemon not reachable on context 'default'",
    ),
]


@pytest.fixture(scope="session")
def built_image() -> str:
    """Build `lotsman:test-l3` once per session, or reuse an existing image."""
    inspect = subprocess.run(
        ["docker", "image", "inspect", TEST_IMAGE],
        capture_output=True,
    )
    if inspect.returncode == 0:
        return TEST_IMAGE

    build = subprocess.run(
        ["docker", "build", "-t", TEST_IMAGE, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if build.returncode != 0:
        tail = (build.stderr or build.stdout or "")[-800:]
        pytest.skip(f"docker build {TEST_IMAGE} failed: {tail}")
    return TEST_IMAGE


def _capability() -> DockerSeaCapability:
    return DockerSeaCapability(
        gpu_model="none",
        gpu_count=0,  # CPU-only test image
        vram_gb=0,
        fp64_native=False,
        cpu_ghz=4.0,
        cpu_cores=4,
        ram_gb=8,
        disk_gb=50,
    )


def _wait_grpc_ready(target: str, timeout_s: float = 20.0) -> None:
    """Poll Whoami until the in-container Lotsman is reachable."""
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            channel = grpc.insecure_channel(target)
            stub = lotsman_pb2_grpc.LotsmanServiceStub(channel)
            stub.Whoami(lotsman_pb2.WhoamiRequest(), timeout=2)
            channel.close()
            return
        except grpc.RpcError as e:
            last_err = e
            time.sleep(0.3)
    raise AssertionError(
        f"Lotsman gRPC not ready at {target} after {timeout_s}s; last: {last_err!r}"
    )


def _force_remove_container(name: str) -> None:
    """Best-effort cleanup that ignores errors."""
    subprocess.run(
        ["docker", "--context", DEFAULT_CONTEXT, "rm", "-f", name],
        capture_output=True,
    )


def test_docker_sea_search_returns_one_offer(built_image: str) -> None:
    """The plumbing of search() through Hub against a real Sea."""
    sea = DockerSea("local", docker_context=DEFAULT_CONTEXT, capability=_capability())
    hub = Hub(seas=[sea])
    [offer] = hub.sea_search("local")
    assert offer.sea == "local"
    assert offer.gpu_count == 0


def test_docker_sea_status_reports_reachable(built_image: str) -> None:
    sea = DockerSea("local", docker_context=DEFAULT_CONTEXT, capability=_capability())
    hub = Hub(seas=[sea])
    s = hub.sea_status("local")
    assert s.reachable is True
    assert s.detail.startswith("docker ")


def test_docker_sea_full_lifecycle(built_image: str) -> None:
    """host_create → Whoami → run echo → Logs → host_destroy.

    The acceptance test for M2-A: the abstraction actually composes into a
    working provisioning flow against real Docker.
    """
    sea = DockerSea("local", docker_context=DEFAULT_CONTEXT, capability=_capability())
    hub = Hub(seas=[sea])
    container_name = "lotsman-l3-lifecycle"

    # Pre-cleanup in case a prior failed run left it behind.
    _force_remove_container(container_name)

    handle = None
    try:
        handle = hub.host_create("local", image=built_image, name=container_name)
        assert handle.state == "running"
        assert handle.sea == "local"
        assert handle.grpc_target.startswith("127.0.0.1:")
        assert container_name in hub.host_list()
        assert hub.host_list(sea="local") == [container_name]

        _wait_grpc_ready(handle.grpc_target)

        # Whoami pierces all the way into the container
        whoami = hub.whoami(container_name)
        assert whoami.lotsman_version != ""

        # Run a short job
        run_resp = hub.run(host=container_name, script="echo l3-smoke\n")
        assert run_resp.job_id.startswith(f"{container_name}/") or "/" in run_resp.job_id

        # Wait for terminal state
        deadline = time.time() + 15
        final_state = None
        while time.time() < deadline:
            s = hub.status(run_resp.job_id)
            if s.state in (lotsman_pb2.DONE, lotsman_pb2.FAILED, lotsman_pb2.KILLED):
                final_state = s.state
                break
            time.sleep(0.1)
        assert final_state == lotsman_pb2.DONE, f"job did not DONE: state={final_state}"

        # Verify stdout
        logs = hub.logs(run_resp.job_id)
        assert logs.stdout.decode().strip() == "l3-smoke"

        # Sea sees the host
        [inst] = sea.list_instances()
        assert inst.name == container_name
        assert inst.state == "running"
    finally:
        if handle is not None:
            try:
                hub.host_destroy(container_name, kill_running=True)
            except Exception:
                pass
        _force_remove_container(container_name)
        hub.shutdown()

    # Post-condition: registry empty, sea inventory empty
    assert hub.host_list() == []
    assert sea.list_instances() == []
