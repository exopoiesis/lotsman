"""L2 service tests for DockerSea: subprocess dispatch via FakeRunner.

We feed scripted (argv → RunResult) responses to DockerSea and assert the
Sea calls `docker --context <ctx> ...` with the right argv shape and updates
its in-memory host registry correctly.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from marina.seas.docker_sea import DockerSea, DockerSeaCapability, SeaError
from marina.seas.runner import RunResult

pytestmark = pytest.mark.service


# ---- fake runner ----


@dataclass
class FakeRunnerCall:
    argv: list[str]
    timeout: float | None


class FakeRunner:
    """Scripted runner: each entry is (matcher, RunResult or fn).

    Order matters: the first matching scripted entry is consumed and removed.
    """

    def __init__(self) -> None:
        self.calls: list[FakeRunnerCall] = []
        self._scripts: list[
            tuple[Callable[[list[str]], bool], RunResult | Callable[[list[str]], RunResult]]
        ] = []

    def expect(
        self,
        matcher: Callable[[list[str]], bool],
        result: RunResult | Callable[[list[str]], RunResult],
    ) -> None:
        self._scripts.append((matcher, result))

    def __call__(self, argv: list[str], *, timeout: float | None = None) -> RunResult:
        self.calls.append(FakeRunnerCall(argv=list(argv), timeout=timeout))
        for i, (matcher, result) in enumerate(self._scripts):
            if matcher(argv):
                self._scripts.pop(i)
                if callable(result):
                    return result(argv)
                return result
        raise AssertionError(f"FakeRunner: unexpected argv {argv!r}")


def _has_subcommand(*tokens: str) -> Callable[[list[str]], bool]:
    expected = set(tokens)

    def _matcher(argv: list[str]) -> bool:
        return expected.issubset(set(argv))

    return _matcher


def _gomer_capability(**overrides: Any) -> DockerSeaCapability:
    base: dict[str, Any] = dict(
        gpu_model="RTX 4070",
        gpu_count=1,
        vram_gb=12,
        fp64_native=False,
        cpu_ghz=5.7,
        cpu_cores=8,
        ram_gb=32,
        disk_gb=500,
        price_per_hour=0.0,
    )
    base.update(overrides)
    return DockerSeaCapability(**base)


def _make_sea(
    runner: FakeRunner,
    *,
    name: str = "gomer",
    docker_context: str = "gomer",
    capability: DockerSeaCapability | None = None,
    clock: Callable[[], float] | None = None,
) -> DockerSea:
    return DockerSea(
        name,
        docker_context=docker_context,
        capability=capability or _gomer_capability(),
        runner=runner,
        clock=clock or (lambda: 1700000000.0),
    )


_INSPECT_PORTS_OK = (
    '{"50051/tcp":[{"HostIp":"0.0.0.0","HostPort":"49234"}]}'
)


# ---- create ----


def test_create_dispatches_docker_run_with_context_and_gpus() -> None:
    runner = FakeRunner()
    runner.expect(
        _has_subcommand("run", "--context", "gomer", "exopoiesis/lotsman:latest"),
        RunResult(returncode=0, stdout="abc123def456\n", stderr=""),
    )
    runner.expect(
        _has_subcommand("inspect"),
        RunResult(returncode=0, stdout=_INSPECT_PORTS_OK, stderr=""),
    )

    sea = _make_sea(runner)
    handle = sea.create("exopoiesis/lotsman:latest", name="gomer-1")

    assert handle.name == "gomer-1"
    assert handle.sea == "gomer"
    assert handle.instance_id == "abc123def456"
    assert handle.grpc_target == "127.0.0.1:49234"
    assert handle.state == "running"
    assert handle.created_at_unix_ms == 1700000000000

    run_call = runner.calls[0]
    assert run_call.argv[:5] == ["docker", "--context", "gomer", "run", "-d"]
    assert "--gpus" in run_call.argv
    assert "all" in run_call.argv
    # explicit name wired through
    assert "--name" in run_call.argv
    assert "gomer-1" in run_call.argv
    # publishes random host port to container 50051
    assert "0:50051" in run_call.argv
    # has both labels
    assert "lotsman_managed=1" in run_call.argv
    assert "lotsman_sea=gomer" in run_call.argv
    # threads host name into the container as ENV (lotsman_pb2-side)
    assert "LOTSMAN_HOST_ID=gomer-1" in run_call.argv
    # exec lotsman serve at end with --host-id so jobIds match Marina's name
    assert run_call.argv[-5:] == [
        "exopoiesis/lotsman:latest",
        "lotsman",
        "serve",
        "--host-id",
        "gomer-1",
    ]


def test_create_no_gpu_flag_when_capability_has_no_gpu() -> None:
    runner = FakeRunner()
    runner.expect(
        _has_subcommand("run"),
        RunResult(returncode=0, stdout="cid\n", stderr=""),
    )
    runner.expect(
        _has_subcommand("inspect"),
        RunResult(returncode=0, stdout=_INSPECT_PORTS_OK, stderr=""),
    )

    cpu_only_cap = _gomer_capability(gpu_count=0, vram_gb=0)
    sea = _make_sea(runner, name="loki", docker_context="default", capability=cpu_only_cap)
    sea.create("ubuntu:latest", name="loki-1")

    run_call = runner.calls[0]
    assert "--gpus" not in run_call.argv


def test_create_auto_names_when_name_omitted() -> None:
    runner = FakeRunner()
    runner.expect(_has_subcommand("run"), RunResult(0, "id1\n", ""))
    runner.expect(_has_subcommand("inspect"), RunResult(0, _INSPECT_PORTS_OK, ""))
    runner.expect(_has_subcommand("run"), RunResult(0, "id2\n", ""))
    runner.expect(_has_subcommand("inspect"), RunResult(0, _INSPECT_PORTS_OK, ""))

    sea = _make_sea(runner)
    h1 = sea.create("img:tag")
    h2 = sea.create("img:tag")

    assert h1.name == "gomer-1"
    assert h2.name == "gomer-2"


def test_create_rejects_invalid_name() -> None:
    sea = _make_sea(FakeRunner())
    with pytest.raises(SeaError, match="invalid host name"):
        sea.create("img", name="bad name with spaces")


def test_create_rejects_duplicate_name() -> None:
    runner = FakeRunner()
    runner.expect(_has_subcommand("run"), RunResult(0, "id\n", ""))
    runner.expect(_has_subcommand("inspect"), RunResult(0, _INSPECT_PORTS_OK, ""))

    sea = _make_sea(runner)
    sea.create("img", name="dup")
    with pytest.raises(SeaError, match="already exists"):
        sea.create("img", name="dup")


def test_create_propagates_docker_failure() -> None:
    runner = FakeRunner()
    runner.expect(
        _has_subcommand("run"),
        RunResult(returncode=125, stdout="", stderr="image not found"),
    )
    sea = _make_sea(runner)
    with pytest.raises(SeaError, match=r"docker run failed.*image not found"):
        sea.create("missing:tag", name="gomer-1")
    # failed create must NOT register host
    assert sea.list_instances() == []


def test_create_fails_when_inspect_returns_no_port() -> None:
    runner = FakeRunner()
    runner.expect(_has_subcommand("run"), RunResult(0, "cid\n", ""))
    runner.expect(
        _has_subcommand("inspect"),
        RunResult(returncode=0, stdout='{"50051/tcp":null}', stderr=""),
    )
    sea = _make_sea(runner)
    with pytest.raises(SeaError, match="no host binding"):
        sea.create("img", name="gomer-1")


# ---- destroy ----


def test_destroy_dispatches_docker_rm_and_unregisters() -> None:
    runner = FakeRunner()
    runner.expect(_has_subcommand("run"), RunResult(0, "cid\n", ""))
    runner.expect(_has_subcommand("inspect"), RunResult(0, _INSPECT_PORTS_OK, ""))
    runner.expect(_has_subcommand("rm", "gomer-1"), RunResult(0, "", ""))

    sea = _make_sea(runner)
    sea.create("img", name="gomer-1")
    sea.destroy("gomer-1")

    rm_call = runner.calls[-1]
    assert "rm" in rm_call.argv
    # plain destroy = no force flag
    assert "-f" not in rm_call.argv
    assert sea.list_instances() == []


def test_destroy_with_kill_running_passes_force() -> None:
    runner = FakeRunner()
    runner.expect(_has_subcommand("run"), RunResult(0, "cid\n", ""))
    runner.expect(_has_subcommand("inspect"), RunResult(0, _INSPECT_PORTS_OK, ""))
    runner.expect(_has_subcommand("rm", "gomer-1", "-f"), RunResult(0, "", ""))

    sea = _make_sea(runner)
    sea.create("img", name="gomer-1")
    sea.destroy("gomer-1", kill_running=True)


def test_destroy_unknown_host_raises() -> None:
    sea = _make_sea(FakeRunner())
    with pytest.raises(SeaError, match="unknown host"):
        sea.destroy("never-existed")


def test_destroy_keeps_host_when_docker_rm_fails() -> None:
    runner = FakeRunner()
    runner.expect(_has_subcommand("run"), RunResult(0, "cid\n", ""))
    runner.expect(_has_subcommand("inspect"), RunResult(0, _INSPECT_PORTS_OK, ""))
    runner.expect(
        _has_subcommand("rm"),
        RunResult(returncode=1, stdout="", stderr="container in use"),
    )

    sea = _make_sea(runner)
    sea.create("img", name="gomer-1")
    with pytest.raises(SeaError, match="container in use"):
        sea.destroy("gomer-1")
    # still there for retry
    assert {h.name for h in sea.list_instances()} == {"gomer-1"}


# ---- stop / start ----


def test_stop_marks_host_stopped() -> None:
    runner = FakeRunner()
    runner.expect(_has_subcommand("run"), RunResult(0, "cid\n", ""))
    runner.expect(_has_subcommand("inspect"), RunResult(0, _INSPECT_PORTS_OK, ""))
    runner.expect(_has_subcommand("stop", "gomer-1"), RunResult(0, "", ""))

    sea = _make_sea(runner)
    sea.create("img", name="gomer-1")
    sea.stop("gomer-1")

    [h] = sea.list_instances()
    assert h.state == "stopped"


def test_start_re_resolves_port_after_restart() -> None:
    runner = FakeRunner()
    runner.expect(_has_subcommand("run"), RunResult(0, "cid\n", ""))
    runner.expect(_has_subcommand("inspect"), RunResult(0, _INSPECT_PORTS_OK, ""))
    runner.expect(_has_subcommand("stop"), RunResult(0, "", ""))
    runner.expect(_has_subcommand("start"), RunResult(0, "", ""))
    runner.expect(
        _has_subcommand("inspect"),
        RunResult(
            returncode=0,
            stdout='{"50051/tcp":[{"HostIp":"0.0.0.0","HostPort":"55555"}]}',
            stderr="",
        ),
    )

    sea = _make_sea(runner)
    sea.create("img", name="gomer-1")
    sea.stop("gomer-1")
    sea.start("gomer-1")

    [h] = sea.list_instances()
    assert h.state == "running"
    assert h.grpc_target == "127.0.0.1:55555"


# ---- status ----


def test_status_reports_reachable_when_docker_info_ok() -> None:
    runner = FakeRunner()
    runner.expect(
        _has_subcommand("info"),
        RunResult(returncode=0, stdout="24.0.7\n", stderr=""),
    )
    sea = _make_sea(runner)
    s = sea.status()
    assert s.reachable is True
    assert "docker 24.0.7" in s.detail


def test_status_reports_unreachable_when_docker_info_fails() -> None:
    runner = FakeRunner()
    runner.expect(
        _has_subcommand("info"),
        RunResult(returncode=1, stdout="", stderr="cannot connect"),
    )
    sea = _make_sea(runner)
    s = sea.status()
    assert s.reachable is False
    assert "cannot connect" in s.detail
