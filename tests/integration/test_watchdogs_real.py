"""L3 integration: watchdog firing inside a real Docker container.

Skipped when local Docker is unreachable. Reuses the lotsman:test-l3 image
(built session-scoped by the docker-real test or built here on demand).
A fresh container is launched with `LOTSMAN_DISK_LOW_GB=99999999` and
`LOTSMAN_DISK_LOW_INTERVAL_S=1` so the disk_low watchdog fires within a
couple of seconds, validating the whole stack:

    Marina  →  Hub.host_create  →  DockerSea  →  docker run -e ...
            →  Lotsman picks up env at startup
            →  Supervisor ticks every WATCHDOG_TICK_S (1s)
            →  DiskLowCheck runs every interval_sec (1s here)
            →  Event fires, recorded in _event_log
            →  Marina pulls via Hub.events_all → EventsHistoryAll RPC
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
def watchdog_image() -> str:
    """Build/refresh `lotsman:test-l3` for this test (sources changed)."""
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
        gpu_count=0,
        vram_gb=0,
        fp64_native=False,
        cpu_ghz=4.0,
        cpu_cores=4,
        ram_gb=8,
        disk_gb=50,
    )


def _force_remove(name: str) -> None:
    subprocess.run(
        ["docker", "--context", DEFAULT_CONTEXT, "rm", "-f", name],
        capture_output=True,
    )


def _wait_grpc_ready(target: str, timeout_s: float = 20.0) -> None:
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


def test_watchdog_list_through_real_container(watchdog_image: str) -> None:
    """Defaults (disk_low / process_oom / gpu_idle) are present after Run."""
    sea = DockerSea("local", docker_context=DEFAULT_CONTEXT, capability=_capability())
    hub = Hub(seas=[sea])
    container_name = "lotsman-l3-wd-list"
    _force_remove(container_name)

    try:
        handle = hub.host_create("local", image=watchdog_image, name=container_name)
        _wait_grpc_ready(handle.grpc_target)

        run = hub.run(host=container_name, script="sleep 5\n")
        wd = hub.watchdog_list(run.job_id)
        names = sorted(w.name for w in wd.watchdogs)
        assert names == ["disk_low", "gpu_idle", "process_oom"]
    finally:
        try:
            hub.host_destroy(container_name, kill_running=True)
        except Exception:
            pass
        _force_remove(container_name)
        hub.shutdown()


def test_disk_low_watchdog_fires_via_env_override(watchdog_image: str) -> None:
    """The full pipeline: Marina sets env -> Lotsman applies -> watchdog fires."""
    sea = DockerSea("local", docker_context=DEFAULT_CONTEXT, capability=_capability())
    hub = Hub(seas=[sea])
    container_name = "lotsman-l3-wd-fire"
    _force_remove(container_name)

    try:
        handle = hub.host_create(
            "local",
            image=watchdog_image,
            name=container_name,
            env={
                # Force fire: any free space < 99 999 999 GB → fires.
                "LOTSMAN_DISK_LOW_GB": "99999999",
                # Tick fast so the test isn't slow: check every 1s.
                "LOTSMAN_DISK_LOW_INTERVAL_S": "1",
            },
        )
        _wait_grpc_ready(handle.grpc_target)

        run = hub.run(host=container_name, script="sleep 8\n")

        # Wait up to 10s for the disk_low fire.
        deadline = time.time() + 10
        fire_found = False
        while time.time() < deadline:
            agg = hub.events_all(hosts=[container_name])
            events = agg.get(container_name, [])
            for ev in events:
                if ev.watchdog_name == "disk_low":
                    fire_found = True
                    assert ev.job_id == run.job_id
                    assert ev.event_type == "watchdog_fired"
                    assert ev.severity == "notify"
                    assert "free=" in ev.detail
                    break
            if fire_found:
                break
            time.sleep(0.3)

        assert fire_found, "disk_low never fired despite forced threshold"

        # WatchdogList should also reflect the fire status now.
        wd = hub.watchdog_list(run.job_id)
        disk_entry = next(w for w in wd.watchdogs if w.name == "disk_low")
        assert disk_entry.fired is True
    finally:
        try:
            hub.host_destroy(container_name, kill_running=True)
        except Exception:
            pass
        _force_remove(container_name)
        hub.shutdown()
