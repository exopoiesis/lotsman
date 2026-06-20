"""Shared sea_search filter helpers (offer_filters) — Vast-parity vocabulary."""
from __future__ import annotations

import pytest

from marina.seas.base import Offer
from marina.seas.offer_filters import apply_common_filters, cpu_patterns

pytestmark = pytest.mark.unit


def _offer(**kw: object) -> Offer:
    base: dict[str, object] = dict(
        sea="x", offer_id="1", gpu_model="A100 SXM4", gpu_count=1, vram_gb=80,
        fp64_native=True, cpu_ghz=7.0, cpu_cores=16, ram_gb=64, disk_gb=200,
        price_per_hour=1.0, cpu_name="AMD Ryzen Threadripper PRO 5955WX",
        cuda_max_good=12.4, reliability=0.98,
    )
    base.update(kw)
    return Offer(**base)  # type: ignore[arg-type]


def test_cpu_patterns_family_and_literal() -> None:
    assert cpu_patterns("trpro")[0].startswith("Threadripper PRO")
    assert cpu_patterns("5955WX") == ["5955WX"]
    assert cpu_patterns("") == []


def test_cpu_name_family_trpro_matches() -> None:
    trpro = _offer(cpu_name="AMD Ryzen Threadripper PRO 5955WX")
    epyc = _offer(offer_id="2", cpu_name="AMD EPYC 7763")
    kept = apply_common_filters([trpro, epyc], {"cpu_name": "trpro"})
    assert [o.offer_id for o in kept] == ["1"]  # only the Threadripper PRO


def test_gpu_name_substring_and_vram() -> None:
    a100 = _offer(gpu_model="A100 SXM4", vram_gb=80)
    h100 = _offer(offer_id="2", gpu_model="H100 PCIE", vram_gb=80)
    assert {o.offer_id for o in apply_common_filters([a100, h100], {"gpu_name": "A100"})} == {"1"}
    assert apply_common_filters([a100], {"vram_gb": 40}) == []  # exact mismatch


def test_min_cuda_rejects_known_low_passes_unknown() -> None:
    lo = _offer(cuda_max_good=11.8, price_per_hour=2.0)       # known too low
    hi = _offer(offer_id="2", cuda_max_good=12.8, price_per_hour=0.5)  # known ok
    unk = _offer(offer_id="3", cuda_max_good=0.0)             # unknown -> passes
    kept = {o.offer_id for o in apply_common_filters([lo, hi, unk], {"min_cuda": 12.4})}
    assert kept == {"2", "3"}  # known-low dropped; known-ok + unknown kept


def test_max_dph() -> None:
    lo = _offer(price_per_hour=2.0)
    hi = _offer(offer_id="2", price_per_hour=0.5)
    assert {o.offer_id for o in apply_common_filters([lo, hi], {"max_dph": 1.0})} == {"2"}


def test_min_reliability_rejects_known_low_passes_unknown() -> None:
    good = _offer(reliability=0.97)                  # known ok
    bad = _offer(offer_id="2", reliability=0.40)     # known too low -> dropped
    unknown = _offer(offer_id="3", reliability=None)  # no data -> benefit of doubt
    kept = {o.offer_id for o in apply_common_filters(
        [good, bad, unknown], {"min_reliability": 0.9})}
    assert kept == {"1", "3"}  # curated/unknown passes; known-low excluded
