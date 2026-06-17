from __future__ import annotations

from dataclasses import dataclass

from marina.seas.base import Offer


@dataclass(frozen=True)
class WorkloadPreset:
    """Hard requirements for a named workload.

    `matches()` does the boolean check; ranking is a Sea concern.
    Constraint values follow project lessons (DEADLY_MISTAKES, PROJECT_STATE):
    paper-grade DFT *requires* native FP64 (A100/H100/V100), MLIP screening
    works on RTX 4070, and so on.
    """

    name: str
    description: str
    requires_fp64: bool = False
    min_vram_gb: int = 0
    min_cpu_ghz: float = 0.0
    min_cpu_cores: int = 0
    min_ram_gb: int = 0
    min_disk_gb: int = 0
    min_reliability: float | None = None
    # How to rank the offers that pass the gate. GPU-FP64 DFT -> "zgpu";
    # CPU DFT (CP2K) -> "zcpu"; MLIP (FP32) -> "dlperf"; or "price".
    rank_by: str = "zgpu"


PRESETS: dict[str, WorkloadPreset] = {
    "dft_paper_grade": WorkloadPreset(
        name="dft_paper_grade",
        description=(
            "Paper-grade DFT (QE/CP2K/ABACUS): A100/H100/V100 only — "
            "native FP64, GHz>=5.0, reliability>=0.95"
        ),
        requires_fp64=True,
        min_vram_gb=16,
        min_cpu_ghz=5.0,
        min_cpu_cores=6,
        min_ram_gb=16,
        min_disk_gb=80,
        min_reliability=0.95,
    ),
    "dft_smoke": WorkloadPreset(
        name="dft_smoke",
        description="Cheap DFT smoke runs; H100 PCIe OK, looser reliability",
        requires_fp64=True,
        min_vram_gb=12,
        min_cpu_ghz=4.5,
        min_cpu_cores=4,
        min_ram_gb=12,
        min_disk_gb=40,
        min_reliability=0.92,
    ),
    "mlip": WorkloadPreset(
        name="mlip",
        description="MLIP screening (MACE/CHGNet); FP32 fine, RTX 4090/L40 OK",
        requires_fp64=False,
        min_vram_gb=12,
        min_cpu_ghz=3.5,
        min_cpu_cores=4,
        min_ram_gb=12,
        min_disk_gb=20,
        rank_by="dlperf",  # MLIP is FP32 — FP64-based zgpu would mis-rank
    ),
    "aimd_long": WorkloadPreset(
        name="aimd_long",
        description=(
            "Multi-week AIMD; paper-grade GPU + big disk + verified host"
        ),
        requires_fp64=True,
        min_vram_gb=16,
        min_cpu_ghz=5.0,
        min_cpu_cores=6,
        min_ram_gb=24,
        min_disk_gb=200,
        min_reliability=0.97,
    ),
}


def matches(offer: Offer, preset: WorkloadPreset) -> bool:
    """True iff the offer meets every hard constraint of the preset."""
    if preset.requires_fp64 and not offer.fp64_native:
        return False
    if offer.vram_gb < preset.min_vram_gb:
        return False
    if offer.cpu_ghz < preset.min_cpu_ghz:
        return False
    if offer.cpu_cores < preset.min_cpu_cores:
        return False
    if offer.ram_gb < preset.min_ram_gb:
        return False
    if offer.disk_gb < preset.min_disk_gb:
        return False
    if preset.min_reliability is not None:
        if offer.reliability is None or offer.reliability < preset.min_reliability:
            return False
    return True
