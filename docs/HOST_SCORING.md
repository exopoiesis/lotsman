# Host search & scoring (VastSea)

How Marina searches the Vast.ai marketplace and how it scores a host's fitness
for DFT, using only an offer's advertised configuration — no benchmarking.

All of this is exposed through the MCP `sea_search` tool and computed in
`marina.seas.vast_sea` / `marina.seas.perf_score`.

---

## 1. Searching

`sea_search(sea, limit, gpu_name, vram_gb, cpu_name, min_reliability, max_dph,
verified, order)`.

Cheap, expressible constraints are pushed into the Vast query; the rest
(`vram_gb`, `cpu_name`, `order`) are applied in Python over a widened fetch pool
(up to 500 offers, so a sort isn't limited to the cheapest few).

### GPU family matching (`gpu_name`)

Vast lists accelerators by sub-model ("A100 SXM4", "Tesla V100"), so a bare
family name never matches `gpu_name=A100`. `gpu_name="A100"` expands to
`gpu_name in [A100_PCIE,A100_SXM4]` via a curated map; a specific model
("A100 SXM4") still matches exactly. A substring guard drops any uncatalogued
variant Vast returns.

Known families: A100, A800, H100, H200, V100, P100, B200. Edit `_GPU_FAMILIES`.

### CPU family matching (`cpu_name`)

Vast does not expose `cpu_name` as a queryable field, so it is matched
python-side as a case-insensitive substring. `cpu_name="trpro"` expands to AMD
Threadripper PRO 7/5/3 WX — the high-clock workstation family the project uses
for CP2K (uniform cores, 8 memory channels). Any other value is a literal
substring ("5955WX", "EPYC 7763"). Edit `_CPU_FAMILIES`.

### Sorting (`order`)

Sort key with optional `-` prefix for descending. Keys: `cpu_ghz`,
`dph`/`price`, `vram_gb`, `cpu_cores`, `dlperf`, `dlperf_per_dollar`,
`gpu_mem_bw`, `pcie_bw`, and the synthetic `zcpu` / `zgpu` (below).
Example: `order="-zgpu"` = best GPU-DFT host first.

### Recommend (workload presets)

`sea_recommend(sea, workload, budget_per_hour, min_hours, rank_by)` applies a
named preset's **hard gate** (the project DEADLY_MISTAKES bar: FP64 / VRAM /
GHz / cores / RAM / disk / reliability — see `presets.py`) and then **ranks the
survivors by host fitness**, not raw price:

- `dft_paper_grade` / `dft_smoke` / `aimd_long` → ranked by `zgpu` (GPU FP64).
- `mlip` → ranked by `dlperf` (MLIP is FP32; ranking by FP64 `zgpu` would
  mis-rank — a 4090 is great for MLIP but poor at FP64).

Override per call with `rank_by` (`zgpu` / `zcpu` / `dlperf` /
`dlperf_per_dollar` / `price`) — e.g. `rank_by="zcpu"` when the paper-grade host
is for a CPU-bound CP2K run. Same table output as `sea_search`.

### Result format

`sea_search` and `sea_recommend` return a **ready-to-display aligned text
table** by default — rendered server-side so the agent spends no tokens parsing
JSON or re-formatting. Fixed columns, contract ID first:

```
ID | GPU | VRAM | CUDA | CPU | cores | RAM | Disk | zGPU | zCPU | DLP/$ | vbw | PCIe | $/hr | geo
```

- **ID** — the Vast `offer_id`; name it to rent (`host_create(offer_id=...)`).
- **CUDA** — `cuda_max_good`, the highest CUDA toolkit the host runs well; match
  it to the image you deploy (filterable via `min_cuda`).
- **cores** — `cpu_cores`/`cpu_cores_total` = cores rented to us / on the whole
  host. A thin slice (e.g. 16/128) gets a small share of memory bandwidth, which
  is why two GPU-identical hosts can have very different `zCPU`.
- **PCIe** (`pcie_bw_gbs`) — measured host↔GPU bandwidth, GB/s (~12 PCIe3,
  ~25 PCIe4, ~50 PCIe5); affects GPU offload.
- **DLP/$** (`dlperf_per_dollar`) — Vast's own FP16/tensor perf per $/hr;
  general bang-for-buck, *not* FP64 — use zGPU for DFT.
- **vbw** (`gpu_mem_bw_gbs`) — measured VRAM bandwidth, GB/s.

Pass `format="json"` for the raw per-offer dicts, which additionally include
`fp64_native`, `cpu_ghz`, `cpu_cores`/`cpu_cores_total`, `inet_down_mbps`,
`dlperf`. `reliability` is parsed and usable as the `min_reliability` filter but
is not shown — we never analysed it as a column.

---

## 2. Performance scores: zCPU and zGPU

Two "parrot" scores derived purely from the advertised config, each normalised
so a reference host scores ~100. They are estimates for *ranking*, not absolute
throughput. Implemented in `marina/seas/perf_score.py`; constants are public
datasheet figures and are meant to be tuned.

### Inputs (all from the Vast offer, not invented)

- Measured by Vast: `gpu_mem_bw` (per-GPU VRAM bandwidth, GB/s), `pcie_bw`
  (host↔GPU, GB/s), `total_flops` (aggregate **FP32** TFLOPS), `cpu_cores` /
  `cpu_cores_effective`, `cpu_ghz`, `cpu_name`, `num_gpus`.
- Estimated by lookup table (datasheet): per-GPU **FP64** TFLOPS by model; host
  RAM bandwidth and core homogeneity by CPU family.

### zCPU — CPU-bound DFT (CP2K / Gaussian-plane-wave)

CP2K is memory-bandwidth bound: cores beyond what the RAM channels can feed are
wasted. A roofline knee:

```
our_bw       = mem_bw(cpu_family) * cores_eff / cores_total       # our share, GB/s
cores_useful = min(cores_eff, our_bw / BW_PER_CORE_GBS, CORE_KNEE)
zCPU         = 100 * homogeneity * cores_useful * cpu_ghz / ZCPU_REF
```

Two caps on how many cores actually count:

- `BW_PER_CORE_GBS = 12` — GB/s a core needs before it starves. On a *shared*
  host our bandwidth share is proportional to our core fraction, so a thin slice
  of a big box (e.g. 16 of 128 cores) can't feed many cores and scores low.
- `CORE_KNEE = 8` — CP2K stops scaling past ~8 cores (measured: beyond ~12 adds
  only ~1-2%), so cores past the knee do **not** raise zCPU. An 8-, 16- or
  64-core well-fed host of the same clock all score the same.

Other terms:

- `homogeneity` — 1.0 for uniform server cores (Threadripper PRO, EPYC, Xeon),
  0.92 for multi-CCD Ryzen, 0.82 for hybrid P+E consumer Intel.
- Reference (~100): a host that fully feeds the 8-core knee at ~7 GHz (e.g. a
  Threadripper PRO 5955WX renting ≥8 of its cores).

### zGPU — GPU-bound DFT (Quantum ESPRESSO)

QE needs FP64; the FP32 `total_flops` Vast reports is the wrong precision, so
consumer cards (≈1/64 FP64) score low however many you stack. A geometric blend
of FP64 throughput and VRAM bandwidth, with sublinear multi-GPU scaling and a
PCIe throttle:

```
fp64_gpu = datasheet FP64 by model, else (total_flops / num_gpus) / 64
eff_gpus = num_gpus ** GPU_SCALE                      # 0.8, sublinear
F        = eff_gpus * fp64_gpu
M        = eff_gpus * gpu_mem_bw
pcie     = clamp(pcie_bw / PCIE_REF, 0.4, 1.0)        # thin PCIe throttles offload
zGPU     = 100 * pcie * (F / F_REF)**W_GPU_FP64 * (M / M_REF)**(1 - W_GPU_FP64)
```

- `W_GPU_FP64 = 0.65` (FP64 weighted over VRAM bandwidth), `GPU_SCALE = 0.8`.
- Reference (~100): one A100 PCIe (FP64 9.7 TFLOPS, ~1935 GB/s VRAM, PCIe ~12).
- `gpu_mem_bw` is Vast's **measured** bandwidth (typically below the datasheet
  peak) — honest, if conservative.
- Non-FP64 GPUs return 0; consumer cards return a small non-zero score (e.g. an
  8×2080 Ti box scores well below a single A100 for FP64 DFT — as it should).

### Tuning

Adjust the module constants — `BW_PER_CORE_GBS`, `W_GPU_FP64`, `GPU_SCALE`, the
`*_REF` references, and the `_GPU_FP64_TFLOPS` / `_CPU_MEM_BW` /
`_CPU_HOMOGENEITY` tables — to taste. Relative ordering is what matters.

### Validation

Sanity-checked over ~800 live offers: `zgpu` ranking is led by FP64 datacenter
GPUs (A100, V100), `zcpu` by Threadripper PRO and 12-channel DDR5 EPYC (Genoa);
consumer GPUs sit low for FP64 work despite high FP32/VRAM figures.

---

## 3. Operational notes

- **Secrets**: the Vast API key is read from `VAST_API_KEY`, loaded at startup
  from a gitignored `.env` next to `marina.toml` (e.g. `~/.lotsman/.env`) — see
  `marina/dotenv.py`. Never in the TOML in plaintext.
- **`vastai_bin`**: pin the full path to the `vastai` CLI in `marina.toml` if
  the MCP launcher's PATH omits the per-user Python Scripts directory.
- **No hangs**: every `vastai` call runs under `cmd_timeout_s` (default 45 s)
  and collects output via temp files rather than pipes, so a wedged or
  shim-spawned child can't hang the MCP server. `create()` also fails fast when
  an instance's `status_msg` shows a doomed startup (bad image / registry auth /
  out of disk) instead of polling until the 30-min ready timeout.

### skypilot interop (bridge until Lotsman's gRPC layer is done)

Marina's SSH identity (`~/.ssh/id_vast`) is the **same keypair** as the legacy
skypilot relay's `id_ed25519`, and both use the **same Vast.ai account**
(verified: identical pubkey fingerprint + API key). So a host created by Marina
is fully usable through skypilot too:

- `create()` runs `vastai attach ssh <id> <id_vast.pub>`, so the instance trusts
  the private key skypilot already holds — `docker exec skypilot ssh …` / `scp`
  work against it.
- skypilot's `vastai` sees the same instances (one account).

Practical split today: drive **discovery / create / destroy** through Marina
(zCPU/zGPU ranking, lifecycle), and run the actual compute over the existing
skypilot SSH/scp relay until Lotsman's in-container gRPC (`lotsman serve` →
run/logs/harvest) is shipped. This interop is a direct consequence of the
"Marina owns its own SSH identity" design — a freshly generated key would not
let skypilot in. Caveat: destroying an instance directly via skypilot/`vastai`
leaves Marina's view stale until the next `host_list`, which reconciles from
instance labels.
