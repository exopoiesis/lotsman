"""Shared sea_search filter vocabulary, applied python-side.

Vast applies these as a mix of server-side `vastai` query tokens and python-side
post-filters. The REST seas (Verda, Clore) have no server-side filtering, so they
apply the whole common set here via `apply_common_filters` — giving every sea the
same `sea_search` semantics (gpu_name / cpu_name family-aware / vram_gb / min_cuda
/ min_reliability / max_dph). A constraint a sea has no data for (e.g. Verda
exposes no CPU model or reliability) simply excludes its offers — honest, since
the constraint can't be shown to hold.
"""
from __future__ import annotations

from marina.seas.base import Offer

# CPU families matched as case-insensitive substrings of an offer's cpu_name.
# A family expands to several patterns (newest gen first); an unknown value is a
# single literal substring (e.g. "5955WX"). Shared so every sea matches "trpro".
_CPU_FAMILIES: dict[str, tuple[str, ...]] = {
    # AMD Threadripper PRO WX workstation chips — the high-clock family the
    # project settled on for CP2K/DFT (e.g. 5955WX ~7 GHz). Gens 7 > 5 > 3.
    "TRPRO": ("Threadripper PRO 7", "Threadripper PRO 5", "Threadripper PRO 3"),
    "TR_PRO": ("Threadripper PRO 7", "Threadripper PRO 5", "Threadripper PRO 3"),
    "THREADRIPPER_PRO": (
        "Threadripper PRO 7", "Threadripper PRO 5", "Threadripper PRO 3",
    ),
}


def cpu_patterns(cpu_name: object) -> list[str]:
    """Substring patterns for a cpu_name filter, family-aware.

    ``trpro`` -> the Threadripper PRO 7/5/3 patterns; any other value is a single
    literal substring (e.g. ``5955WX`` or ``EPYC 7763``); empty -> none.
    """
    if not cpu_name:
        return []
    name = str(cpu_name).strip()
    fam = _CPU_FAMILIES.get(name.upper().replace(" ", "_"))
    return list(fam) if fam else [name]


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def apply_common_filters(
    offers: list[Offer], filters: dict[str, object] | None
) -> list[Offer]:
    """Apply Vast's python-side sea_search filter set to an offer list.

    Two kinds of filter, treated differently when a sea lacks the data:

    * **Selection axes** — ``gpu_name`` (substring; a bare family like "A100"
      already matches "A100 SXM4"), ``cpu_name`` (family-aware via
      `cpu_patterns`), ``vram_gb`` (exact per-GPU): you asked for a specific
      thing, so an offer that can't show it is excluded.
    * **Quality gates** — ``min_cuda`` (``cuda_max_good`` >=) and
      ``min_reliability`` (>=): these reject offers KNOWN to fall short, but an
      offer with no value (a curated datacenter like Verda exposes neither
      CUDA-max nor a reliability score) gets the benefit of the doubt and PASSES
      — its homogeneous fleet satisfies the gate by construction, so excluding
      it would be backwards. Vast/Clore report both, so they're gated normally.

    ``max_dph`` (<=) always applies. Does NOT apply ``order`` / ``host_type`` /
    ``limit`` — those are handled by each sea / the caller.
    """
    f = filters or {}

    gpu = str(f.get("gpu_name") or "").strip().upper()
    if gpu:
        offers = [o for o in offers if gpu in o.gpu_model.upper()]

    pats = [p.lower() for p in cpu_patterns(f.get("cpu_name"))]
    if pats:
        offers = [o for o in offers if any(p in o.cpu_name.lower() for p in pats)]

    if f.get("vram_gb"):
        vram = _as_int(f["vram_gb"])
        offers = [o for o in offers if o.vram_gb == vram]

    if f.get("min_cuda"):
        min_cuda = _as_float(f["min_cuda"])
        # Quality gate: reject only KNOWN-too-low; unknown (<=0) passes.
        offers = [o for o in offers
                  if o.cuda_max_good <= 0 or o.cuda_max_good >= min_cuda]

    if f.get("min_reliability"):
        min_rel = _as_float(f["min_reliability"])
        # Quality gate: reject only KNOWN-too-low; unknown (None) passes.
        offers = [o for o in offers
                  if o.reliability is None or o.reliability >= min_rel]

    if f.get("max_dph"):
        cap = _as_float(f["max_dph"])
        offers = [o for o in offers if 0 < o.price_per_hour <= cap]

    return offers
