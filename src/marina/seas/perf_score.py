"""Semi-objective host performance scores for DFT, from a Vast offer's
advertised configuration alone — no benchmarking.

Two "parrot" scores, each normalised so a reference host scores ~100:

- **zCPU** — CPU-bound DFT (CP2K / Gaussian-plane-wave). A *roofline knee*: how
  many cores the host's memory bandwidth can actually feed, times clock. CP2K is
  bandwidth-bound, so cores beyond what the RAM channels can feed are wasted —
  this is exactly why a balanced 8-channel Threadripper PRO beats a 64-core part
  starved on the same 8 channels. Memory bandwidth is estimated from the CPU
  family (channels x DRAM rate); Vast does not advertise it.

- **zGPU** — GPU-bound DFT (QE). A geometric blend of *per-GPU* FP64 throughput
  and VRAM bandwidth, with sublinear multi-GPU scaling and a PCIe throttle. FP64
  (not the FP32 `total_flops` Vast reports) is what double-precision DFT needs,
  so consumer cards — strong FP32, ~1/64 FP64 — score low however many you stack.

Constants are public datasheet figures (FP64 TFLOPS, memory channels x DRAM
rate); approximate by design. Tune the WEIGHTS / *_REF / knee constants — the
relative ordering is the point, not the absolute value.
"""
from __future__ import annotations

# ---- tunables ----
BW_PER_CORE_GBS = 12.0  # zCPU knee: GB/s one core needs before it starves (CP2K)
CORE_KNEE = 8.0         # zCPU: CP2K saturates here; more cores add ~nothing (~1-2%)
GPU_SCALE = 0.8         # zGPU: multi-GPU scaling exponent (sublinear for QE)
W_GPU_FP64 = 0.65       # zGPU: FP64 throughput vs VRAM bandwidth

# ---- reference hosts (define the ~100 point) ----
_ZCPU_REF = CORE_KNEE * 7.0  # a host that fully feeds the knee at ~7.0 GHz = 100
_F_REF = 9.7       # FP64 TFLOPS of one A100
_M_REF = 1935.0    # A100 PCIe VRAM bandwidth (GB/s)
_PCIE_REF = 12.0   # ~PCIe 4.0 x16 effective (Vast measures below the 25 GB/s peak)

# Single-GPU FP64 (double-precision vector) TFLOPS, public datasheet values.
# Matched as a case-insensitive substring of the Vast gpu_name (first hit wins).
_GPU_FP64_TFLOPS: tuple[tuple[str, float], ...] = (
    ("H200", 34.0), ("H100", 30.0), ("H800", 1.0),   # H800 FP64 export-capped
    ("GH200", 34.0), ("B200", 40.0), ("B100", 40.0),
    ("A800", 9.7), ("A100", 9.7),
    ("V100", 7.0), ("P100", 4.7),
)
# Consumer cards expose ~1/64 of FP32 as FP64; derive per-GPU from total_flops.
_CONSUMER_FP64_RATIO = 1.0 / 64.0

# CPU memory bandwidth (GB/s, whole host) by family substring, newest first.
# channels x DRAM rate: TR PRO/EPYC = 8-12 ch, consumer = 2 ch.
_CPU_MEM_BW: tuple[tuple[str, float], ...] = (
    ("Threadripper PRO 7", 333.0),  # 8-ch DDR5-5200
    ("Threadripper PRO 9", 333.0),
    ("Threadripper PRO 5", 205.0),  # 8-ch DDR4-3200
    ("Threadripper PRO 3", 205.0),
    ("Threadripper", 100.0),         # non-PRO: 4-ch DDR4
    ("EPYC 9", 460.0),               # Genoa: 12-ch DDR5
    ("EPYC", 205.0),                 # Rome/Milan: 8-ch DDR4
    ("Xeon", 140.0),                 # mixed; 6-ch DDR4 typical
    ("Ryzen", 70.0),                 # 2-ch
    ("Core", 70.0),                  # 2-ch
)
_DEFAULT_CPU_MEM_BW = 100.0

# Core homogeneity multiplier (uniform cores schedule + scale better for MPI/OMP
# — the reason the project picked Threadripper PRO over hybrid consumer parts).
_CPU_HOMOGENEITY: tuple[tuple[str, float], ...] = (
    ("Threadripper", 1.0), ("EPYC", 1.0), ("Xeon", 1.0),  # uniform
    ("Ryzen", 0.92),                                       # cross-CCD latency
    ("Core", 0.82),                                        # P+E hybrid (12th gen+)
)
_DEFAULT_HOMOGENEITY = 0.95


def _lookup(name: str, table: tuple[tuple[str, float], ...], default: float) -> float:
    upper = name.upper()
    for token, value in table:
        if token.upper() in upper:
            return value
    return default


def fp64_tflops(
    gpu_name: str, total_flops: float = 0.0, num_gpus: int = 1
) -> float:
    """Per-GPU FP64 TFLOPS: datasheet for known models, else FP32/64 fallback.

    `total_flops` is Vast's *aggregate* FP32 across all GPUs, so the consumer
    fallback divides by `num_gpus` to get a per-GPU figure.
    """
    if not gpu_name:
        return 0.0
    upper = gpu_name.upper()
    for token, value in _GPU_FP64_TFLOPS:
        if token in upper:
            return value
    per_gpu_fp32 = max(total_flops, 0.0) / max(num_gpus, 1)
    return per_gpu_fp32 * _CONSUMER_FP64_RATIO


def cpu_mem_bandwidth(cpu_name: str) -> float:
    """Estimated whole-host memory bandwidth (GB/s) from the CPU family."""
    return _lookup(cpu_name or "", _CPU_MEM_BW, _DEFAULT_CPU_MEM_BW)


def cpu_homogeneity(cpu_name: str) -> float:
    """Core-homogeneity multiplier (1.0 = all cores identical)."""
    return _lookup(cpu_name or "", _CPU_HOMOGENEITY, _DEFAULT_HOMOGENEITY)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def zcpu(
    cores_eff: int,
    cores_total: int,
    cpu_ghz: float,
    cpu_name: str,
    *,
    bw_per_core: float = BW_PER_CORE_GBS,
    core_knee: float = CORE_KNEE,
) -> int:
    """CPU-DFT (CP2K) score; ~100 for a host that fully feeds the knee at ~7 GHz.

    Two caps on how many cores actually count:
    - **bandwidth**: our share of host memory bandwidth feeds only so many cores;
    - **saturation** (`core_knee`): CP2K stops scaling past ~8 cores, so extra
      cores add ~nothing (measured ~1-2%) and must not inflate the score.
    """
    if cores_eff <= 0 or cpu_ghz <= 0:
        return 0
    our_bw = cpu_mem_bandwidth(cpu_name) * (cores_eff / max(cores_total, cores_eff))
    cores_fed = our_bw / bw_per_core
    cores_useful = min(float(cores_eff), cores_fed, core_knee)
    score: float = 100.0 * cpu_homogeneity(cpu_name) * cores_useful * cpu_ghz / _ZCPU_REF
    return round(score)


def zgpu(
    gpu_name: str,
    num_gpus: int,
    gpu_mem_bw_gbs: float,
    pcie_bw_gbs: float,
    *,
    total_flops: float = 0.0,
    w_fp64: float = W_GPU_FP64,
    gpu_scale: float = GPU_SCALE,
) -> int:
    """GPU-DFT (QE) score; ~100 for one A100 PCIe. ~0 without a GPU."""
    if num_gpus <= 0 or gpu_mem_bw_gbs <= 0:
        return 0
    per_gpu_fp64 = fp64_tflops(gpu_name, total_flops, num_gpus)
    if per_gpu_fp64 <= 0:
        return 0
    eff_gpus = num_gpus**gpu_scale  # sublinear multi-GPU scaling
    f = eff_gpus * per_gpu_fp64
    m = eff_gpus * gpu_mem_bw_gbs
    # Unknown/zero PCIe: don't penalise (treat as reference).
    pcie = _clamp(pcie_bw_gbs / _PCIE_REF, 0.4, 1.0) if pcie_bw_gbs > 0 else 1.0
    score: float = (
        100.0 * pcie * (f / _F_REF) ** w_fp64 * (m / _M_REF) ** (1.0 - w_fp64)
    )
    return round(score)
