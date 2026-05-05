"""Hub-side fan-out for watchdog events across one or more real Lotsmen.

We spawn one (or two) LotsmanService instances with a deterministic
`_FireOnceCheck` so events appear right after `Run`. Then assert Hub's
watchdog_list / watchdog_history / events / events_all routing and
aggregation behave correctly.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path

import grpc
import pytest

from lotsman.server import LotsmanService
from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc
from lotsman.watchdogs import CheckResult, WatchdogContext
from marina.hub import Hub

pytestmark = pytest.mark.service


@dataclass
class _FireOnceCheck:
    name: str = "scripted"
    interval_sec: float = 0.0

    def check(self, ctx: WatchdogContext) -> CheckResult | None:
        return CheckResult(
            name=self.name, detail="stub fire", severity="notify", data={"k": "v"}
        )


@dataclass
class _Spawned:
    host_id: str
    target: str
    servicer: LotsmanService
    server: grpc.Server


def _spawn(host_id: str, jobs_dir: Path) -> _Spawned:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    servicer = LotsmanService(
        host_id=host_id,
        jobs_dir=jobs_dir,
        default_checks=[_FireOnceCheck()],
    )
    lotsman_pb2_grpc.add_LotsmanServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    return _Spawned(
        host_id=host_id,
        target=f"localhost:{port}",
        servicer=servicer,
        server=server,
    )


def _shutdown(s: _Spawned) -> None:
    s.servicer.shutdown()
    s.server.stop(grace=None)


@pytest.fixture
def two_fire_lotsmen(tmp_path: Path) -> Iterator[tuple[_Spawned, _Spawned]]:
    a = _spawn("hostA", tmp_path / "a")
    (tmp_path / "a").mkdir(exist_ok=True)
    b = _spawn("hostB", tmp_path / "b")
    (tmp_path / "b").mkdir(exist_ok=True)
    try:
        yield a, b
    finally:
        _shutdown(a)
        _shutdown(b)


def _wait_event(stub, job_id: str, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = stub.WatchdogHistory(
            lotsman_pb2.WatchdogHistoryRequest(job_id=job_id)
        )
        if len(resp.events) > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"no event fired for {job_id} within {timeout_s}s")


# ---- Hub.watchdog_list / watchdog_history ----


def test_hub_watchdog_list_routes_by_jobid(two_fire_lotsmen: tuple[_Spawned, _Spawned]) -> None:
    a, b = two_fire_lotsmen
    hub = Hub()
    try:
        hub.host_add(a.host_id, a.target)
        hub.host_add(b.host_id, b.target)

        run_a = hub.run(host=a.host_id, script="echo a\n")
        resp = hub.watchdog_list(run_a.job_id)
        assert [w.name for w in resp.watchdogs] == ["scripted"]
    finally:
        hub.shutdown()


def test_hub_watchdog_history_routes_by_jobid(two_fire_lotsmen: tuple[_Spawned, _Spawned]) -> None:
    a, b = two_fire_lotsmen
    hub = Hub()
    try:
        hub.host_add(a.host_id, a.target)
        hub.host_add(b.host_id, b.target)

        run_a = hub.run(host=a.host_id, script="echo a\n")
        # use raw stub to wait for event without coupling to hub's wait helper
        a_stub = lotsman_pb2_grpc.LotsmanServiceStub(
            grpc.insecure_channel(a.target)
        )
        _wait_event(a_stub, run_a.job_id)

        resp = hub.watchdog_history(run_a.job_id)
        assert len(resp.events) == 1
        assert resp.events[0].watchdog_name == "scripted"
        assert resp.events[0].job_id == run_a.job_id
    finally:
        hub.shutdown()


def test_hub_events_alias_returns_history(two_fire_lotsmen: tuple[_Spawned, _Spawned]) -> None:
    a, _b = two_fire_lotsmen
    hub = Hub()
    try:
        hub.host_add(a.host_id, a.target)
        run = hub.run(host=a.host_id, script="echo x\n")
        a_stub = lotsman_pb2_grpc.LotsmanServiceStub(
            grpc.insecure_channel(a.target)
        )
        _wait_event(a_stub, run.job_id)
        resp = hub.events(run.job_id)
        assert len(resp.events) == 1
    finally:
        hub.shutdown()


# ---- Hub.events_all (fan-out) ----


def test_events_all_fans_out_across_hosts(two_fire_lotsmen: tuple[_Spawned, _Spawned]) -> None:
    a, b = two_fire_lotsmen
    hub = Hub()
    try:
        hub.host_add(a.host_id, a.target)
        hub.host_add(b.host_id, b.target)

        run_a = hub.run(host=a.host_id, script="echo a\n")
        run_b = hub.run(host=b.host_id, script="echo b\n")

        # Wait for both fires to record on their respective Lotsmen.
        for stub_target, jid in (
            (a.target, run_a.job_id),
            (b.target, run_b.job_id),
        ):
            stub = lotsman_pb2_grpc.LotsmanServiceStub(grpc.insecure_channel(stub_target))
            _wait_event(stub, jid)

        agg = hub.events_all()
        assert set(agg.keys()) == {a.host_id, b.host_id}
        assert len(agg[a.host_id]) == 1
        assert len(agg[b.host_id]) == 1
        assert agg[a.host_id][0].job_id == run_a.job_id
        assert agg[b.host_id][0].job_id == run_b.job_id
    finally:
        hub.shutdown()


def test_events_all_filters_by_host(two_fire_lotsmen: tuple[_Spawned, _Spawned]) -> None:
    a, b = two_fire_lotsmen
    hub = Hub()
    try:
        hub.host_add(a.host_id, a.target)
        hub.host_add(b.host_id, b.target)

        run_a = hub.run(host=a.host_id, script="echo a\n")
        a_stub = lotsman_pb2_grpc.LotsmanServiceStub(grpc.insecure_channel(a.target))
        _wait_event(a_stub, run_a.job_id)

        agg = hub.events_all(hosts=[a.host_id])
        assert list(agg) == [a.host_id]
    finally:
        hub.shutdown()


def test_events_all_since_filter_drops_old_events(
    two_fire_lotsmen: tuple[_Spawned, _Spawned],
) -> None:
    a, _b = two_fire_lotsmen
    hub = Hub()
    try:
        hub.host_add(a.host_id, a.target)
        run = hub.run(host=a.host_id, script="echo a\n")
        a_stub = lotsman_pb2_grpc.LotsmanServiceStub(grpc.insecure_channel(a.target))
        _wait_event(a_stub, run.job_id)

        future_ms = int(time.time() * 1000) + 1_000_000
        agg = hub.events_all(since_unix_ms=future_ms, hosts=[a.host_id])
        assert agg == {a.host_id: []}
    finally:
        hub.shutdown()


def test_events_all_swallows_unreachable_host() -> None:
    """A dead host must not poison aggregate; it contributes []."""
    hub = Hub()
    try:
        # bogus port nothing listens on
        hub.host_add("ghost", "127.0.0.1:1")
        agg = hub.events_all(hosts=["ghost"])
        assert agg == {"ghost": []}
    finally:
        hub.shutdown()
