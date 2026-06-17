from __future__ import annotations

import pytest

from marina.seas.perf_score import (
    cpu_homogeneity,
    cpu_mem_bandwidth,
    fp64_tflops,
    zcpu,
    zgpu,
)

pytestmark = pytest.mark.unit


def test_fp64_known_models_vs_consumer_fallback() -> None:
    assert fp64_tflops("A100 SXM4") == 9.7
    assert fp64_tflops("Tesla V100") == 7.0
    assert fp64_tflops("H100 SXM") == 30.0
    # consumer: ~1/64 of the reported FP32 total_flops
    assert fp64_tflops("RTX 4090", total_flops=64.0) == pytest.approx(1.0)
    assert fp64_tflops("", total_flops=100.0) == 0.0


def test_cpu_mem_bandwidth_and_homogeneity_by_family() -> None:
    assert cpu_mem_bandwidth("AMD Ryzen Threadripper PRO 5955WX") == 205.0
    assert cpu_mem_bandwidth("AMD Ryzen Threadripper PRO 7975WX") == 333.0
    assert cpu_mem_bandwidth("AMD EPYC 9654") == 460.0
    assert cpu_mem_bandwidth("Intel Core i9-13900K") == 70.0
    # uniform server parts score full homogeneity; hybrid consumer is penalised
    assert cpu_homogeneity("Threadripper PRO 5955WX") == 1.0
    assert cpu_homogeneity("Intel Core i9-13900K") == 0.82


def test_zcpu_reference_is_about_100() -> None:
    # a host that fully feeds the 8-core knee at ~7 GHz is the ~100 reference
    z = zcpu(16, 16, 7.0, "AMD Ryzen Threadripper PRO 5955WX")
    assert 90 <= z <= 110
    assert zcpu(0, 16, 7.0, "x") == 0


def test_zcpu_cores_beyond_knee_dont_inflate() -> None:
    # 8, 16, 64 cores of the same well-fed host all score the same (CP2K
    # saturates ~8); core count past the knee must not raise zCPU.
    name = "AMD Ryzen Threadripper PRO 5955WX"
    z8 = zcpu(8, 16, 7.0, name)
    assert zcpu(16, 16, 7.0, name) == z8
    assert zcpu(64, 64, 7.0, name) == z8


def test_zcpu_thin_bandwidth_slice_is_penalised() -> None:
    # 16 of 128 cores on a shared EPYC gets a small share of memory bandwidth,
    # so it cannot feed the knee and scores well below a dedicated host.
    thin = zcpu(16, 128, 3.7, "AMD EPYC 7713 64-Core Processor")
    full = zcpu(16, 16, 3.7, "AMD EPYC 7713 64-Core Processor")
    assert thin < full


def test_zgpu_reference_and_consumer_gap() -> None:
    # one A100 PCIE near the ~100 reference
    z_a100 = zgpu("A100 PCIE", 1, 1935.0, 12.0)
    assert 80 <= z_a100 <= 120
    # two A100s clearly stronger
    assert zgpu("A100 PCIE", 2, 1935.0, 12.0) > z_a100
    # a consumer card (tiny FP64) scores far below an A100
    z_4090 = zgpu("RTX 4090", 1, 1008.0, 12.0, total_flops=82.0)
    assert z_4090 < z_a100 / 3
    # no GPU -> 0
    assert zgpu("", 0, 0.0, 0.0) == 0
