from __future__ import annotations

import pytest

from marina.seas.base import Offer
from marina.seas.presets import PRESETS, WorkloadPreset, matches

pytestmark = pytest.mark.unit


def _gomer_offer(**overrides: object) -> Offer:
    base: dict[str, object] = dict(
        sea="gomer",
        offer_id="gomer-local",
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
    return Offer(**base)  # type: ignore[arg-type]


def _vast_a100_offer(**overrides: object) -> Offer:
    base: dict[str, object] = dict(
        sea="vast",
        offer_id="12345",
        gpu_model="A100 SXM4",
        gpu_count=1,
        vram_gb=80,
        fp64_native=True,
        cpu_ghz=5.2,
        cpu_cores=8,
        ram_gb=64,
        disk_gb=400,
        price_per_hour=0.85,
        reliability=0.97,
    )
    base.update(overrides)
    return Offer(**base)  # type: ignore[arg-type]


def test_known_preset_names() -> None:
    assert {"dft_paper_grade", "dft_smoke", "mlip", "aimd_long"} <= set(PRESETS)


def test_dft_paper_grade_rejects_no_fp64() -> None:
    assert matches(_gomer_offer(), PRESETS["dft_paper_grade"]) is False


def test_dft_paper_grade_accepts_a100() -> None:
    assert matches(_vast_a100_offer(), PRESETS["dft_paper_grade"]) is True


def test_mlip_accepts_rtx4070() -> None:
    assert matches(_gomer_offer(), PRESETS["mlip"]) is True


def test_dft_paper_grade_rejects_low_ghz() -> None:
    offer = _vast_a100_offer(cpu_ghz=4.0)
    assert matches(offer, PRESETS["dft_paper_grade"]) is False


def test_dft_paper_grade_rejects_low_reliability() -> None:
    offer = _vast_a100_offer(reliability=0.90)
    assert matches(offer, PRESETS["dft_paper_grade"]) is False


def test_dft_paper_grade_rejects_no_reliability() -> None:
    offer = _vast_a100_offer(reliability=None)
    assert matches(offer, PRESETS["dft_paper_grade"]) is False


def test_mlip_no_reliability_requirement() -> None:
    offer = _gomer_offer()
    assert offer.reliability is None
    assert matches(offer, PRESETS["mlip"]) is True


def test_aimd_long_rejects_small_disk() -> None:
    offer = _vast_a100_offer(disk_gb=100)
    assert matches(offer, PRESETS["aimd_long"]) is False


def test_aimd_long_accepts_big_a100() -> None:
    offer = _vast_a100_offer(disk_gb=400)
    assert matches(offer, PRESETS["aimd_long"]) is True


def test_dft_smoke_accepts_lower_reliability() -> None:
    offer = _vast_a100_offer(reliability=0.93)
    assert matches(offer, PRESETS["dft_smoke"]) is True
    assert matches(offer, PRESETS["dft_paper_grade"]) is False


def test_workload_preset_constructible_directly() -> None:
    p = WorkloadPreset(
        name="custom",
        description="x",
        min_vram_gb=8,
        requires_fp64=False,
    )
    assert p.requires_fp64 is False
    assert p.min_cpu_cores == 0
