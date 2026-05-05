from __future__ import annotations

import time

import pytest

from lotsman.v1 import lotsman_pb2
from marina.hub import HostError, Hub

pytestmark = pytest.mark.service


def _wait_done(hub, job_id, timeout_s=5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        s = hub.status(job_id)
        if s.state in (lotsman_pb2.DONE, lotsman_pb2.FAILED, lotsman_pb2.KILLED):
            return s
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_hub_host_add_lists_host(lotsman_tcp):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        assert hub.host_list() == [lotsman_tcp.host_id]
    finally:
        hub.shutdown()


def test_hub_host_add_duplicate_raises(lotsman_tcp):
    hub = Hub()
    try:
        hub.host_add("ws-1", lotsman_tcp.target)
        with pytest.raises(HostError):
            hub.host_add("ws-1", lotsman_tcp.target)
    finally:
        hub.shutdown()


def test_hub_run_proxies_to_lotsman(lotsman_tcp):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        resp = hub.run(host=lotsman_tcp.host_id, script="echo hub-test\n")
        assert resp.job_id.startswith(f"{lotsman_tcp.host_id}/")
        assert resp.state == lotsman_pb2.RUNNING
        _wait_done(hub, resp.job_id)
    finally:
        hub.shutdown()


def test_hub_unknown_host_raises(lotsman_tcp):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        with pytest.raises(HostError):
            hub.run(host="ghost-host", script="echo x\n")
    finally:
        hub.shutdown()


def test_hub_routes_status_by_jobid(two_lotsmen):
    h1, h2 = two_lotsmen
    hub = Hub()
    try:
        hub.host_add(h1.host_id, h1.target)
        hub.host_add(h2.host_id, h2.target)

        r1 = hub.run(host=h1.host_id, script="echo from-1\n")
        r2 = hub.run(host=h2.host_id, script="echo from-2\n")

        s1 = hub.status(r1.job_id)
        s2 = hub.status(r2.job_id)
        assert s1.job_id.startswith(h1.host_id + "/")
        assert s2.job_id.startswith(h2.host_id + "/")
        assert s1.job_id != s2.job_id
    finally:
        hub.shutdown()


def test_hub_logs_via_jobid(lotsman_tcp):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        r = hub.run(host=lotsman_tcp.host_id, script="echo hello\n")
        _wait_done(hub, r.job_id)

        resp = hub.logs(r.job_id)
        assert resp.stdout == b"hello\n"
    finally:
        hub.shutdown()


def test_hub_kill_via_jobid(lotsman_tcp):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        r = hub.run(host=lotsman_tcp.host_id, script="sleep 30\n")
        kill_resp = hub.kill(r.job_id, grace_sec=1.0)
        assert kill_resp.killed is True
        assert kill_resp.state == lotsman_pb2.KILLED
    finally:
        hub.shutdown()


def test_hub_whoami_via_host_name(lotsman_tcp):
    hub = Hub()
    try:
        hub.host_add(lotsman_tcp.host_id, lotsman_tcp.target)
        resp = hub.whoami(lotsman_tcp.host_id)
        assert resp.lotsman_version != ""
    finally:
        hub.shutdown()
