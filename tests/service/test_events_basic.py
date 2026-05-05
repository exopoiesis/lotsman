"""L2 service tests for Events / WatchdogList / WatchdogHistory.

Each test stands up a LotsmanService with a deterministic stub check
(`_FireOnceCheck`) so we can assert fire/history/stream semantics without
real disk pressure or GPU hardware.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import pytest

from lotsman.server import LotsmanService
from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc
from lotsman.watchdogs import CheckResult, WatchdogContext

pytestmark = pytest.mark.service


# ---- a check that fires deterministically on first tick ----


@dataclass
class _FireOnceCheck:
    name: str = "scripted"
    interval_sec: float = 0.0
    detail: str = "scripted fire"

    def check(self, ctx: WatchdogContext) -> CheckResult | None:
        return CheckResult(
            name=self.name,
            detail=self.detail,
            severity="notify",
            data={"key": "val"},
        )


@dataclass
class _NeverFireCheck:
    name: str = "quiet"
    interval_sec: float = 0.0

    def check(self, ctx: WatchdogContext) -> CheckResult | None:
        return None


# ---- fixture: spawn a real LotsmanService over an ephemeral TCP gRPC port ----


@dataclass
class _Spawned:
    stub: lotsman_pb2_grpc.LotsmanServiceStub
    servicer: LotsmanService
    server: grpc.Server
    channel: grpc.Channel


def _spawn(checks: list[Any], jobs_dir: Path) -> _Spawned:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    servicer = LotsmanService(
        host_id="ws",
        jobs_dir=jobs_dir,
        default_checks=checks,
    )
    lotsman_pb2_grpc.add_LotsmanServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    channel = grpc.insecure_channel(f"localhost:{port}")
    stub = lotsman_pb2_grpc.LotsmanServiceStub(channel)
    return _Spawned(stub=stub, servicer=servicer, server=server, channel=channel)


def _shutdown(s: _Spawned) -> None:
    s.channel.close()
    s.servicer.shutdown()
    s.server.stop(grace=None)


@pytest.fixture
def fire_once_lotsman(tmp_path: Path) -> Iterator[_Spawned]:
    s = _spawn([_FireOnceCheck()], tmp_path)
    try:
        yield s
    finally:
        _shutdown(s)


@pytest.fixture
def quiet_lotsman(tmp_path: Path) -> Iterator[_Spawned]:
    s = _spawn([_NeverFireCheck()], tmp_path)
    try:
        yield s
    finally:
        _shutdown(s)


# ---- WatchdogList ----


def test_watchdog_list_returns_attached_checks(quiet_lotsman: _Spawned) -> None:
    run = quiet_lotsman.stub.Run(lotsman_pb2.RunRequest(script="echo hi\n"))
    resp = quiet_lotsman.stub.WatchdogList(
        lotsman_pb2.WatchdogListRequest(job_id=run.job_id)
    )
    names = [w.name for w in resp.watchdogs]
    assert names == ["quiet"]
    assert resp.watchdogs[0].fired is False
    assert resp.watchdogs[0].action == "notify"


def test_watchdog_list_unknown_job_returns_not_found(quiet_lotsman: _Spawned) -> None:
    with pytest.raises(grpc.RpcError) as exc:
        quiet_lotsman.stub.WatchdogList(
            lotsman_pb2.WatchdogListRequest(job_id="no/such")
        )
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


def test_watchdog_list_marks_fired_after_event(fire_once_lotsman: _Spawned) -> None:
    run = fire_once_lotsman.stub.Run(lotsman_pb2.RunRequest(script="echo hi\n"))
    _wait_until_fired(fire_once_lotsman, run.job_id)

    resp = fire_once_lotsman.stub.WatchdogList(
        lotsman_pb2.WatchdogListRequest(job_id=run.job_id)
    )
    [wd] = resp.watchdogs
    assert wd.name == "scripted"
    assert wd.fired is True


# ---- WatchdogHistory ----


def test_watchdog_history_records_fire(fire_once_lotsman: _Spawned) -> None:
    run = fire_once_lotsman.stub.Run(lotsman_pb2.RunRequest(script="echo hi\n"))
    _wait_until_fired(fire_once_lotsman, run.job_id)

    resp = fire_once_lotsman.stub.WatchdogHistory(
        lotsman_pb2.WatchdogHistoryRequest(job_id=run.job_id)
    )
    [event] = resp.events
    assert event.job_id == run.job_id
    assert event.watchdog_name == "scripted"
    assert event.event_type == "watchdog_fired"
    assert event.severity == "notify"
    assert event.unix_ms > 0
    assert dict(event.data) == {"key": "val"}


def test_watchdog_history_unknown_job_returns_not_found(fire_once_lotsman: _Spawned) -> None:
    with pytest.raises(grpc.RpcError) as exc:
        fire_once_lotsman.stub.WatchdogHistory(
            lotsman_pb2.WatchdogHistoryRequest(job_id="missing")
        )
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


def test_watchdog_history_filters_by_since_unix_ms(fire_once_lotsman: _Spawned) -> None:
    run = fire_once_lotsman.stub.Run(lotsman_pb2.RunRequest(script="echo hi\n"))
    _wait_until_fired(fire_once_lotsman, run.job_id)

    # Far-future cutoff drops everything.
    far_future = int(time.time() * 1000) + 1_000_000_000
    resp = fire_once_lotsman.stub.WatchdogHistory(
        lotsman_pb2.WatchdogHistoryRequest(
            job_id=run.job_id, since_unix_ms=far_future
        )
    )
    assert list(resp.events) == []


# ---- Events streaming ----


def test_events_stream_yields_live_fires(fire_once_lotsman: _Spawned) -> None:
    """Subscribe before Run; the fire event arrives over the stream."""
    started = time.time()
    sub_iter = fire_once_lotsman.stub.Events(
        lotsman_pb2.EventsRequest(job_id=""),  # all jobs
        timeout=5,
    )

    # Give the stream a moment to subscribe
    time.sleep(0.05)
    run = fire_once_lotsman.stub.Run(lotsman_pb2.RunRequest(script="echo hi\n"))

    received: list[lotsman_pb2.Event] = []
    deadline = time.time() + 5
    try:
        for event in sub_iter:
            received.append(event)
            if event.job_id == run.job_id and event.watchdog_name == "scripted":
                break
            if time.time() > deadline:
                break
    except grpc.RpcError:
        pass

    assert any(
        e.job_id == run.job_id and e.watchdog_name == "scripted" for e in received
    ), f"no scripted event received in {time.time() - started:.1f}s; got {received}"


def test_events_replays_history_when_since_set(fire_once_lotsman: _Spawned) -> None:
    """Subscribe AFTER firing with `since_unix_ms` from before — gets replay."""
    before = int(time.time() * 1000) - 1
    run = fire_once_lotsman.stub.Run(lotsman_pb2.RunRequest(script="echo hi\n"))
    _wait_until_fired(fire_once_lotsman, run.job_id)

    sub_iter = fire_once_lotsman.stub.Events(
        lotsman_pb2.EventsRequest(job_id=run.job_id, since_unix_ms=before),
        timeout=5,
    )

    received: list[lotsman_pb2.Event] = []
    try:
        for event in sub_iter:
            received.append(event)
            if event.watchdog_name == "scripted":
                break
    except grpc.RpcError:
        pass

    assert len(received) >= 1
    assert received[0].watchdog_name == "scripted"


def test_events_no_replay_when_since_zero(quiet_lotsman: _Spawned) -> None:
    """since_unix_ms=0 means live-only — empty history is not replayed."""
    quiet_lotsman.stub.Run(lotsman_pb2.RunRequest(script="echo hi\n"))
    sub_iter = quiet_lotsman.stub.Events(
        lotsman_pb2.EventsRequest(job_id="", since_unix_ms=0),
        timeout=1,
    )
    received: list[lotsman_pb2.Event] = []
    try:
        for event in sub_iter:
            received.append(event)
    except grpc.RpcError:
        pass
    assert received == []


# ---- helpers ----


def _wait_until_fired(s: _Spawned, job_id: str, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = s.stub.WatchdogHistory(
            lotsman_pb2.WatchdogHistoryRequest(job_id=job_id)
        )
        if len(resp.events) > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"no watchdog event fired for {job_id} within {timeout_s}s")
