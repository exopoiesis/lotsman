"""L1 pure-logic tests for VastSea: offer parsing, query building, fp64 table.

No subprocess at all — these exercise the pure helpers directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from marina.seas.base import Offer
from marina.seas.runner import RunResult
from marina.seas.vast_sea import (
    VastSea,
    VastSeaError,
    _default_pubkey_loader,
    fp64_native,
)

pytestmark = pytest.mark.unit


def test_default_pubkey_loader_reads_and_strips(tmp_path: Path) -> None:
    p = tmp_path / "id_vast.pub"
    p.write_text("ssh-ed25519 AAAAKEY marina@host\n", encoding="utf-8")
    assert _default_pubkey_loader(str(p)) == "ssh-ed25519 AAAAKEY marina@host"


@pytest.mark.parametrize(
    ("gpu", "expected"),
    [
        ("A100 PCIE", True),
        ("A100 SXM4", True),
        ("H100 NVL", True),
        ("V100", True),
        ("Tesla P100", True),
        ("RTX 4090", False),
        ("L40S", False),
        ("A40", False),
        ("RTX 6000Ada", False),
    ],
)
def test_fp64_native_table(gpu: str, expected: bool) -> None:
    assert fp64_native(gpu) is expected


def _sea() -> VastSea:
    # runner unused by these pure helpers; return a valid RunResult to type-check
    return VastSea("vast", api_key="k", runner=lambda *a, **k: RunResult(0, "", ""))


def test_offer_from_raw_proportional_ram_and_units() -> None:
    raw = {
        "id": 7,
        "gpu_name": "A100 SXM4",
        "num_gpus": 2,
        "gpu_ram": 81920,
        "cpu_ghz": 5.0,
        "cpu_name": "AMD EPYC 7763",
        "cpu_cores": 64,
        "cpu_cores_effective": 16,
        "cpu_ram": 262144,
        "disk_space": 512,
        "dph_total": 1.6,
        "reliability2": 0.99,
        "inet_down": 1200,
        "geolocation": "Czechia, CZ",
        "dlperf": 123.4,
        "dlperf_per_dphtotal": 77.1,
    }
    offer = _sea()._offer_from_raw(raw)
    assert offer.offer_id == "7"
    assert offer.gpu_count == 2
    assert offer.vram_gb == 80
    assert offer.cpu_cores == 16  # our effective share
    assert offer.cpu_cores_total == 64  # whole host
    assert offer.cpu_name == "AMD EPYC 7763"
    assert offer.geolocation == "Czechia, CZ"
    assert offer.dlperf == 123.4
    assert offer.dlperf_per_dollar == 77.1  # from dlperf_per_dphtotal
    # 262144MB * 16/64 = 65536MB = 64GB
    assert offer.ram_gb == 64
    assert offer.disk_gb == 512
    assert offer.price_per_hour == 1.6
    assert offer.fp64_native is True
    assert offer.reliability == 0.99
    assert offer.inet_down_mbps == 1200


def test_offer_from_raw_tolerates_missing_fields() -> None:
    offer = _sea()._offer_from_raw({"id": 1, "gpu_name": "RTX 4090"})
    assert offer.cpu_cores == 0
    assert offer.ram_gb == 0
    assert offer.reliability is None
    assert offer.inet_down_mbps is None


def test_build_query_defaults_to_rentable_verified() -> None:
    q = _sea()._build_query(None)
    assert "rentable=true" in q
    assert "verified=true" in q


def test_build_query_verified_can_be_disabled() -> None:
    q = _sea()._build_query({"verified": False})
    assert "verified=true" not in q


def test_build_query_appends_raw_escape_hatch() -> None:
    q = _sea()._build_query({"query": "gpu_arch=hopper"})
    assert "gpu_arch=hopper" in q


def test_build_query_min_cuda_uses_cuda_max_good() -> None:
    q = _sea()._build_query({"min_cuda": 12.8})
    assert "cuda_max_good>=12.8" in q
    assert "cuda_vers" not in q  # not the driver-version field


def test_gpu_name_token_family_expands_to_in_list() -> None:
    tok = VastSea._gpu_name_token("A100")
    assert tok == "gpu_name in [A100_PCIE,A100_SXM4]"
    # case-insensitive family lookup
    assert VastSea._gpu_name_token("a100") == tok


def test_gpu_name_token_specific_model_is_exact() -> None:
    assert VastSea._gpu_name_token("A100 SXM4") == "gpu_name=A100_SXM4"
    assert VastSea._gpu_name_token("") == ""
    assert VastSea._gpu_name_token(None) == ""


def test_cpu_patterns_family_and_literal() -> None:
    # family alias -> Threadripper PRO generations 7/5/3, newest first
    pats = VastSea._cpu_patterns("trpro")
    assert pats == [
        "Threadripper PRO 7", "Threadripper PRO 5", "Threadripper PRO 3"
    ]
    assert VastSea._cpu_patterns("THREADRIPPER PRO") == pats  # space/alias norm
    # unknown value -> single literal substring
    assert VastSea._cpu_patterns("5955WX") == ["5955WX"]
    assert VastSea._cpu_patterns("") == []
    assert VastSea._cpu_patterns(None) == []


def test_sort_offers_descending_by_cpu_ghz() -> None:
    def mk(oid: str, ghz: float) -> Offer:
        return Offer(
            sea="vast", offer_id=oid, gpu_model="A100 PCIE", gpu_count=1,
            vram_gb=40, fp64_native=True, cpu_ghz=ghz, cpu_cores=8,
            ram_gb=32, disk_gb=100, price_per_hour=1.0,
        )

    offers = [mk("a", 3.7), mk("b", 4.0), mk("c", 3.9)]
    ordered = VastSea._sort_offers(offers, "-cpu_ghz")
    assert [o.offer_id for o in ordered] == ["b", "c", "a"]
    # ascending alias by frequency
    asc = VastSea._sort_offers(offers, "ghz")
    assert [o.offer_id for o in asc] == ["a", "c", "b"]


def test_sort_offers_rejects_unknown_key() -> None:
    with pytest.raises(VastSeaError):
        VastSea._sort_offers([], "bogus")


def test_label_round_trip() -> None:
    sea = _sea()
    label = sea._label("w3")
    assert label == "w3"  # short label, just the host name
    assert sea._host_name_from_label(label) == "w3"
    assert sea._host_name_from_label("") is None
    assert sea._host_name_from_label(None) is None
