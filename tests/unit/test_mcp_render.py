from __future__ import annotations

import pytest

from marina.mcp_server import (
    _format_offers_table,
    _rank_offers,
    _short_cpu,
    _sort_merged,
)
from marina.seas.base import Offer
from marina.seas.presets import PRESETS

pytestmark = pytest.mark.unit


def _offer(**kw: object) -> Offer:
    base: dict[str, object] = dict(
        sea="vast", offer_id="123", gpu_model="A100 SXM4", gpu_count=8,
        vram_gb=80, fp64_native=True, cpu_ghz=3.7, cpu_cores=128,
        ram_gb=2003, disk_gb=530, price_per_hour=8.19,
        cpu_name="AMD EPYC 7713 64-Core Processor", cpu_cores_total=128,
        geolocation="Japan, JP", dlperf_per_dollar=105.7,
        gpu_mem_bw_gbs=1672.7, pcie_bw_gbs=22.9, zcpu=57, zgpu=502,
    )
    base.update(kw)
    return Offer(**base)  # type: ignore[arg-type]


def test_short_cpu_strips_noise() -> None:
    assert _short_cpu("AMD EPYC 7713 64-Core Processor") == "EPYC 7713"
    assert _short_cpu("AMD Ryzen Threadripper PRO 5955WX 16-Cores") == "TR PRO 5955WX"
    assert _short_cpu("Xeon® Platinum 8559C") == "Xeon Platinum 8559C"


def test_format_table_has_fixed_columns_and_id_first() -> None:
    table = _format_offers_table([_offer()])
    header = table.splitlines()[0]
    # ID column first, then the agreed fixed set
    assert header.split("|")[0].strip() == "ID"
    for col in ("GPU", "VRAM", "CUDA", "CPU", "cores", "RAM", "Disk", "zGPU",
                "zCPU", "DLP/$", "vbw", "PCIe", "$/hr", "geo"):
        assert col in header
    # the contract id and computed cells show up in the row
    assert "123" in table
    assert "8xA100 SXM4" in table
    assert "502" in table  # zGPU
    assert "EPYC 7713" in table
    assert "128/128" in table  # cores ours/total


def test_format_table_empty() -> None:
    assert _format_offers_table([]) == "(no offers)"


def test_format_table_with_sea_prepends_sea_column() -> None:
    table = _format_offers_table(
        [_offer(sea="vast", offer_id="v1"), _offer(sea="verda", offer_id="d1")],
        with_sea=True,
    )
    header_cells = [c.strip() for c in table.splitlines()[0].split("|")]
    assert header_cells[0] == "sea"        # sea column is first
    assert header_cells[1] == "ID"
    body = "\n".join(table.splitlines()[2:])
    assert "vast" in body and "verda" in body


def test_sort_merged_price_default_and_keyed() -> None:
    a = _offer(sea="vast", offer_id="a", price_per_hour=2.0, zgpu=100)
    b = _offer(sea="verda", offer_id="b", price_per_hour=1.0, zgpu=300)
    c = _offer(sea="clore", offer_id="c", price_per_hour=3.0, zgpu=200)
    ids = lambda offs: [o.offer_id for o in offs]  # noqa: E731
    # default: cheapest first
    assert ids(_sort_merged([a, b, c], "")) == ["b", "a", "c"]
    # descending zgpu
    assert ids(_sort_merged([a, b, c], "-zgpu")) == ["b", "c", "a"]


def test_format_table_host_type_column_before_price() -> None:
    table = _format_offers_table([
        _offer(offer_id="od1", host_type="on-demand", price_per_hour=1.50),
        _offer(offer_id="sp1", host_type="interruptible", price_per_hour=0.50),
    ])
    header_cells = [c.strip() for c in table.splitlines()[0].split("|")]
    # "type" sits third from the right: ..., type, $/hr, geo
    assert header_cells[-3:] == ["type", "$/hr", "geo"]
    body = table.splitlines()[2:]
    assert any("OD" in r and "1.50" in r for r in body)
    assert any("spot" in r and "0.50" in r for r in body)


def test_rank_offers_by_fitness_desc_and_price_asc() -> None:
    a = _offer(offer_id="a", zgpu=100, zcpu=50, dlperf=10, price_per_hour=2.0)
    b = _offer(offer_id="b", zgpu=200, zcpu=20, dlperf=99, price_per_hour=5.0)
    c = _offer(offer_id="c", zgpu=150, zcpu=80, dlperf=1, price_per_hour=1.0)
    ids = lambda offs: [o.offer_id for o in offs]  # noqa: E731
    assert ids(_rank_offers([a, b, c], "zgpu")) == ["b", "c", "a"]
    assert ids(_rank_offers([a, b, c], "zcpu")) == ["c", "a", "b"]
    assert ids(_rank_offers([a, b, c], "dlperf")) == ["b", "a", "c"]
    assert ids(_rank_offers([a, b, c], "price")) == ["c", "a", "b"]  # ascending


def test_preset_rank_defaults() -> None:
    # GPU-FP64 DFT ranks by zgpu; MLIP (FP32) by dlperf, not zgpu
    assert PRESETS["dft_paper_grade"].rank_by == "zgpu"
    assert PRESETS["aimd_long"].rank_by == "zgpu"
    assert PRESETS["mlip"].rank_by == "dlperf"
