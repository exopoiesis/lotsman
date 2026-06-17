"""L2 service tests for VastSea: `vastai ... --raw` dispatch via FakeRunner.

We feed scripted (argv -> RunResult) responses to VastSea and assert it shells
out to `vastai` with the right argv shape and parses the JSON back into Offers /
HostHandles. No network, no real Vast.ai account.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from marina.seas.runner import RunResult
from marina.seas.vast_sea import VastSea, VastSeaError

pytestmark = pytest.mark.service


# ---- fake runner (same shape as the DockerSea service test) ----


@dataclass
class FakeRunnerCall:
    argv: list[str]
    timeout: float | None


class FakeRunner:
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
                return result(argv) if callable(result) else result
        raise AssertionError(f"FakeRunner: unexpected argv {argv!r}")


def _has(*tokens: str) -> Callable[[list[str]], bool]:
    expected = set(tokens)
    return lambda argv: expected.issubset(set(argv))


# ---- fake forwarder (no real ssh tunnel) ----


@dataclass
class FakeForward:
    local_port: int
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class FakeForwarder:
    def __init__(self, port: int = 55000) -> None:
        self._port = port
        self.opens: list[dict[str, object]] = []
        self.handed_out: list[FakeForward] = []

    def open(self, **kwargs: object) -> FakeForward:
        self.opens.append(kwargs)
        fwd = FakeForward(local_port=self._port)
        self.handed_out.append(fwd)
        return fwd


def _make_sea(
    runner: FakeRunner, forwarder: FakeForwarder | None = None
) -> VastSea:
    return VastSea(
        "vast",
        api_key="test-key",
        runner=runner,
        forwarder=forwarder or FakeForwarder(),
        clock=lambda: 1700000000.0,
        sleeper=lambda _s: None,
        poll_interval_s=0.0,
        ready_timeout_s=100.0,
        ssh_ready_timeout_s=100.0,
    )


_OFFERS = json.dumps(
    [
        {
            "id": 111,
            "gpu_name": "A100 PCIE",
            "num_gpus": 1,
            "gpu_ram": 81920,
            "cpu_ghz": 5.2,
            "cpu_cores": 64,
            "cpu_cores_effective": 16,
            "cpu_ram": 131072,
            "disk_space": 200,
            "dph_total": 0.75,
            "reliability2": 0.985,
            "inet_down": 950,
            "verified": True,
        },
        {
            "id": 222,
            "gpu_name": "RTX 4090",
            "num_gpus": 1,
            "gpu_ram": 24576,
            "cpu_ghz": 4.0,
            "cpu_cores": 32,
            "cpu_cores_effective": 8,
            "cpu_ram": 65536,
            "disk_space": 100,
            "dph_total": 0.40,
            "reliability2": 0.97,
            "inet_down": 500,
            "verified": True,
        },
    ]
)


# ---- search ----


def test_search_parses_offers_and_marks_fp64() -> None:
    runner = FakeRunner()
    runner.expect(_has("search", "offers"), RunResult(0, _OFFERS, ""))

    sea = _make_sea(runner)
    offers = sea.search()

    assert [o.offer_id for o in offers] == ["111", "222"]
    a100, rtx = offers
    # A100 -> native FP64; vram per GPU GB; allocated cores = effective
    assert a100.fp64_native is True
    assert a100.vram_gb == 80
    assert a100.cpu_cores == 16
    assert a100.gpu_model == "A100 PCIE"
    assert a100.reliability == 0.985
    # RAM allocated proportionally: 131072MB * 16/64 = 32768MB = 32GB
    assert a100.ram_gb == 32
    # consumer card -> no native FP64
    assert rtx.fp64_native is False


def test_search_builds_query_with_filters_and_limit() -> None:
    runner = FakeRunner()
    runner.expect(_has("search", "offers"), RunResult(0, _OFFERS, ""))

    sea = _make_sea(runner)
    sea.search(filters={"min_reliability": 0.95, "max_dph": 0.8, "num_gpus": 2}, limit=1)

    argv = runner.calls[0].argv
    # query string is a single argv token
    query = argv[argv.index("offers") + 1]
    assert "rentable=true" in query
    assert "verified=true" in query
    assert "reliability>0.95" in query
    assert "dph_total<0.8" in query
    assert "num_gpus=2" in query
    # ordered by price, limited, raw JSON requested
    assert "--raw" in argv
    assert argv[argv.index("--order") + 1] == "dph_total"
    assert argv[argv.index("--limit") + 1] == "1"


def test_search_family_gpu_name_expands_and_vram_filters() -> None:
    runner = FakeRunner()
    runner.expect(_has("search", "offers"), RunResult(0, _OFFERS, ""))

    sea = _make_sea(runner)
    # A100 family + 80GB filter: RTX 4090 (24GB) must be dropped, and a family
    # query (not bare gpu_name=A100) must be sent to Vast.
    offers = sea.search(filters={"gpu_name": "A100", "vram_gb": 80}, limit=10)

    argv = runner.calls[0].argv
    query = argv[argv.index("offers") + 1]
    assert "gpu_name in [A100_PCIE,A100_SXM4]" in query
    # python-side filter/sort needs a broad pool, not just `limit`
    assert int(argv[argv.index("--limit") + 1]) >= 500
    assert [o.offer_id for o in offers] == ["111"]  # only the 80GB A100


def test_search_order_sorts_python_side() -> None:
    runner = FakeRunner()
    runner.expect(_has("search", "offers"), RunResult(0, _OFFERS, ""))

    sea = _make_sea(runner)
    # default Vast order is by price (RTX first); -cpu_ghz must re-sort so the
    # 5.2GHz A100 leads the 4.0GHz RTX.
    offers = sea.search(filters={"order": "-cpu_ghz"}, limit=10)
    assert [o.offer_id for o in offers] == ["111", "222"]


def test_search_cpu_name_family_filters_python_side() -> None:
    offers_json = json.dumps([
        {"id": 1, "gpu_name": "A100 SXM4", "num_gpus": 1, "gpu_ram": 40960,
         "cpu_name": "AMD Ryzen Threadripper PRO 5955WX 16-Cores",
         "cpu_cores": 16, "cpu_cores_effective": 16, "dph_total": 2.0},
        {"id": 2, "gpu_name": "A100 SXM4", "num_gpus": 1, "gpu_ram": 40960,
         "cpu_name": "AMD EPYC 7763 64-Core Processor",
         "cpu_cores": 64, "cpu_cores_effective": 8, "dph_total": 1.0},
        {"id": 3, "gpu_name": "A100 PCIE", "num_gpus": 1, "gpu_ram": 40960,
         "cpu_name": "AMD Ryzen Threadripper PRO 7975WX 32-Cores",
         "cpu_cores": 32, "cpu_cores_effective": 16, "dph_total": 3.0},
    ])
    runner = FakeRunner()
    runner.expect(_has("search", "offers"), RunResult(0, offers_json, ""))

    sea = _make_sea(runner)
    offers = sea.search(filters={"cpu_name": "trpro"}, limit=10)

    # only the two Threadripper PRO hosts survive; EPYC dropped
    assert sorted(o.offer_id for o in offers) == ["1", "3"]
    assert offers[0].cpu_name.startswith("AMD Ryzen Threadripper PRO")
    # python-side filter widened the fetch pool
    argv = runner.calls[0].argv
    assert int(argv[argv.index("--limit") + 1]) >= 500


def test_search_times_out_to_clear_error_not_a_hang() -> None:
    def boom(_argv: list[str]) -> RunResult:
        raise subprocess.TimeoutExpired(cmd="vastai", timeout=45.0)

    runner = FakeRunner()
    runner.expect(_has("search", "offers"), boom)

    sea = _make_sea(runner)
    with pytest.raises(VastSeaError, match="timed out"):
        sea.search()
    # and the runner was actually given a finite timeout
    assert runner.calls[0].timeout is not None


def test_search_missing_binary_raises_clear_error() -> None:
    def missing(_argv: list[str]) -> RunResult:
        raise FileNotFoundError(2, "not found")

    runner = FakeRunner()
    runner.expect(_has("search", "offers"), missing)

    sea = _make_sea(runner)
    with pytest.raises(VastSeaError, match="not found on PATH"):
        sea.search()


def test_vastai_bin_override_is_used() -> None:
    runner = FakeRunner()
    runner.expect(_has("search", "offers"), RunResult(0, "[]", ""))

    sea = VastSea(
        "vast", api_key="k", runner=runner,
        forwarder=FakeForwarder(), vastai_bin="/opt/vastai.exe",
    )
    sea.search()
    assert runner.calls[0].argv[0] == "/opt/vastai.exe"


def test_recommend_filters_to_preset_and_sorts_by_price() -> None:
    runner = FakeRunner()
    runner.expect(_has("search", "offers"), RunResult(0, _OFFERS, ""))

    sea = _make_sea(runner)
    # dft_paper_grade requires FP64 -> only the A100 survives matches()
    kept = sea.recommend("dft_paper_grade", budget_per_hour=1.0)

    assert [o.offer_id for o in kept] == ["111"]


def test_recommend_unknown_workload_raises() -> None:
    sea = _make_sea(FakeRunner())
    with pytest.raises(ValueError, match="unknown workload"):
        sea.recommend("nonsense")


# ---- create ----


def _instances(**over: object) -> str:
    entry: dict[str, object] = {
        "id": 99,
        "actual_status": "running",
        "ssh_host": "ssh5.vast.ai",
        "ssh_port": 41022,
        "dph_total": 0.75,
        "label": "w1",
    }
    entry.update(over)
    return json.dumps([entry])


def test_create_rents_offer_waits_running_and_returns_handle() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))
    runner.expect(_has("ssh", "true"), RunResult(0, "", ""))

    forwarder = FakeForwarder(port=55001)
    sea = _make_sea(runner, forwarder)
    handle = sea.create("exopoiesis/infra-qe-gpu:server", offer_id="111", name="w1", disk_gb=200)

    assert handle.name == "w1"
    assert handle.sea == "vast"
    assert handle.instance_id == "99"
    assert handle.state == "running"
    assert handle.cost_per_hour == 0.75
    assert handle.ssh_target == "root@ssh5.vast.ai:41022"
    assert handle.created_at_unix_ms == 1700000000000
    # gRPC target is the local end of the SSH tunnel, not the ssh endpoint
    assert handle.grpc_target == "127.0.0.1:55001"

    create_argv = runner.calls[0].argv
    assert "--image" in create_argv
    assert "exopoiesis/infra-qe-gpu:server" in create_argv
    assert "--disk" in create_argv and "200" in create_argv
    # SSH must be enabled on the instance so the tunnel can attach
    assert "--ssh" in create_argv
    # host name is the label (short) for cross-restart reconciliation
    assert "--label" in create_argv
    assert create_argv[create_argv.index("--label") + 1] == "w1"
    # api key passed but kept out of nothing-else
    assert "--api-key" in create_argv

    # tunnel opened to the container gRPC port over the instance's ssh endpoint
    [opened] = forwarder.opens
    assert opened["ssh_host"] == "ssh5.vast.ai"
    assert opened["ssh_port"] == 41022
    assert opened["remote_port"] == 50051
    assert opened["ssh_user"] == "root"


def test_create_attaches_local_ssh_key_when_configured() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    runner.expect(_has("attach", "ssh", "99"), RunResult(0, "{}", ""))
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))
    runner.expect(_has("ssh", "true"), RunResult(0, "", ""))

    sea = VastSea(
        "vast",
        api_key="test-key",
        runner=runner,
        forwarder=FakeForwarder(),
        clock=lambda: 1700000000.0,
        sleeper=lambda _s: None,
        poll_interval_s=0.0,
        ready_timeout_s=100.0,
        ssh_ready_timeout_s=100.0,
        ssh_key_path="/keys/id_vast",
        pubkey_loader=lambda _p: "ssh-ed25519 AAAAKEYBODY marina@host",
    )
    sea.create("img", offer_id="111", name="w1")

    attach = next(c for c in runner.calls if "attach" in c.argv)
    assert "ssh" in attach.argv
    assert "99" in attach.argv
    assert "ssh-ed25519 AAAAKEYBODY marina@host" in attach.argv


def test_create_skips_attach_when_no_local_key() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))
    runner.expect(_has("ssh", "true"), RunResult(0, "", ""))

    sea = _make_sea(runner)  # no ssh_key_path -> rely on account keys
    sea.create("img", offer_id="111", name="w1")

    assert not any("attach" in c.argv for c in runner.calls)


def test_create_polls_until_running() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    runner.expect(_has("show", "instances"), RunResult(0, _instances(actual_status="loading"), ""))
    runner.expect(_has("show", "instances"), RunResult(0, _instances(actual_status="running"), ""))
    runner.expect(_has("ssh", "true"), RunResult(0, "", ""))

    sea = _make_sea(runner)
    handle = sea.create("img", offer_id="111", name="w1")
    assert handle.state == "running"
    assert handle.grpc_target == "127.0.0.1:55000"


def test_create_retries_ssh_until_ready() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))
    runner.expect(_has("ssh", "true"), RunResult(255, "", "Connection refused"))
    runner.expect(_has("ssh", "true"), RunResult(0, "", ""))

    sea = _make_sea(runner)
    handle = sea.create("img", offer_id="111", name="w1")
    assert handle.grpc_target == "127.0.0.1:55000"


def test_create_requires_offer_id() -> None:
    sea = _make_sea(FakeRunner())
    with pytest.raises(VastSeaError, match="requires offer_id"):
        sea.create("img", name="w1")


def test_create_raises_when_instance_goes_bad() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    runner.expect(_has("show", "instances"), RunResult(0, _instances(actual_status="exited"), ""))

    sea = _make_sea(runner)
    with pytest.raises(VastSeaError, match="exited"):
        sea.create("img", offer_id="111", name="w1")


def test_create_propagates_vastai_failure() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance"),
        RunResult(returncode=1, stdout="", stderr="no such offer"),
    )
    sea = _make_sea(runner)
    with pytest.raises(VastSeaError, match="no such offer"):
        sea.create("img", offer_id="111", name="w1")


# ---- list / destroy / stop / start ----


def test_create_fails_fast_on_bad_image_pull() -> None:
    # A doomed image pull keeps actual_status at "loading" while status_msg
    # carries the daemon error; create must raise at once, not poll to timeout.
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    bad = json.dumps([{
        "id": 99, "actual_status": "loading",
        "status_msg": "Error response from daemon: pull access denied for foo",
    }])
    runner.expect(_has("show", "instances"), RunResult(0, bad, ""))

    sea = _make_sea(runner)
    with pytest.raises(VastSeaError, match="cannot start"):
        sea.create("foo/bar:tag", offer_id="111", name="w1")


def test_list_instances_reconciles_from_labels() -> None:
    runner = FakeRunner()
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))

    sea = _make_sea(runner)
    hosts = sea.list_instances()
    assert [h.name for h in hosts] == ["w1"]
    assert hosts[0].instance_id == "99"


def test_list_instances_ignores_unlabeled() -> None:
    runner = FakeRunner()
    unlabeled = json.dumps([{"id": 5, "actual_status": "running"}])
    runner.expect(_has("show", "instances"), RunResult(0, unlabeled, ""))

    sea = _make_sea(runner)
    assert sea.list_instances() == []


def test_destroy_dispatches_unregisters_and_closes_tunnel() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))
    runner.expect(_has("ssh", "true"), RunResult(0, "", ""))
    runner.expect(_has("destroy", "instance", "99"), RunResult(0, "{}", ""))

    forwarder = FakeForwarder()
    sea = _make_sea(runner, forwarder)
    sea.create("img", offer_id="111", name="w1")
    sea.destroy("w1")

    assert "destroy" in runner.calls[-1].argv
    assert "w1" not in sea._hosts
    # the SSH tunnel must be torn down on destroy
    assert forwarder.handed_out[0].closed is True


def test_destroy_unknown_host_raises() -> None:
    runner = FakeRunner()
    runner.expect(_has("show", "instances"), RunResult(0, "[]", ""))
    sea = _make_sea(runner)
    with pytest.raises(VastSeaError, match="unknown host"):
        sea.destroy("never")


def test_stop_closes_tunnel_and_start_reopens_it() -> None:
    runner = FakeRunner()
    runner.expect(
        _has("create", "instance", "111"),
        RunResult(0, json.dumps({"success": True, "new_contract": 99}), ""),
    )
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))
    runner.expect(_has("ssh", "true"), RunResult(0, "", ""))
    runner.expect(_has("stop", "instance", "99"), RunResult(0, "{}", ""))
    # start: vastai start -> wait running -> ssh ready -> reopen forward
    runner.expect(_has("start", "instance", "99"), RunResult(0, "{}", ""))
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))
    runner.expect(_has("ssh", "true"), RunResult(0, "", ""))

    forwarder = FakeForwarder()
    sea = _make_sea(runner, forwarder)
    sea.create("img", offer_id="111", name="w1")
    sea.stop("w1")
    assert forwarder.handed_out[0].closed is True  # first tunnel down

    sea.start("w1")
    # a fresh tunnel was opened and is the new gRPC target
    assert len(forwarder.handed_out) == 2
    assert sea._hosts["w1"].grpc_target == "127.0.0.1:55000"


# ---- status / cost / renew ----


def test_status_reports_balance_when_reachable() -> None:
    runner = FakeRunner()
    runner.expect(_has("show", "user"), RunResult(0, json.dumps({"credit": 392.5}), ""))
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))

    sea = _make_sea(runner)
    s = sea.status()
    assert s.reachable is True
    assert s.balance == 392.5
    assert s.burn_rate_per_hour == 0.75


def test_status_unreachable_without_key() -> None:
    sea = VastSea("vast", api_key=None, runner=FakeRunner())
    s = sea.status()
    assert s.reachable is False
    assert "API key" in s.detail


def test_cost_summary_computes_days_remaining() -> None:
    runner = FakeRunner()
    runner.expect(_has("show", "instances"), RunResult(0, _instances(), ""))
    runner.expect(_has("show", "user"), RunResult(0, json.dumps({"credit": 36.0}), ""))

    sea = _make_sea(runner)
    cb = sea.cost_summary()
    assert cb.total_per_hour == 0.75
    assert cb.burn_rate_24h == 18.0
    assert cb.days_remaining_at_balance == 2.0


def test_renew_not_supported() -> None:
    sea = _make_sea(FakeRunner())
    with pytest.raises(NotImplementedError, match="run until destroyed"):
        sea.renew("w1", 24)


def test_json_calls_fail_without_key() -> None:
    sea = VastSea("vast", api_key=None, runner=FakeRunner())
    with pytest.raises(VastSeaError, match="no Vast.ai API key"):
        sea.search()
