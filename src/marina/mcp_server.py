from __future__ import annotations

import base64
import re
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP

from marina.hub import Hub
from marina.seas.presets import PRESETS


def make_mcp_server(hub: Hub, name: str = "Marina") -> FastMCP:
    mcp = FastMCP(name)

    # ---- sea registry / queries ----

    @mcp.tool()
    def sea_list() -> list[str]:
        """All seas (hosting providers) registered with Marina."""
        return hub.sea_list()

    @mcp.tool()
    def sea_search(
        sea: str,
        limit: int = 15,
        gpu_name: str = "",
        vram_gb: int = 0,
        cpu_name: str = "",
        min_cuda: float = 0.0,
        min_reliability: float = 0.0,
        max_dph: float = 0.0,
        verified: bool = True,
        order: str = "",
        host_type: str = "any",
        format: str = "table",
    ) -> str | list[dict[str, Any]]:
        """List offers in a sea, with optional filters and sort.

        Returns a ready-to-display aligned text table by default (top ``limit``
        offers, default 15 — a wide shortlist to choose from). RELAY IT VERBATIM:
        the table is rendered server-side precisely so the agent need not re-read,
        re-rank, or hand-filter the rows (that just burns tokens). Shape the
        result with the params below (``gpu_name``/``vram_gb``/``order``/
        ``host_type``/``max_dph``/``limit``), not with post-hoc filtering.

        Fixed columns: ID, GPU, VRAM, CUDA, CPU, cores, RAM, Disk, zGPU, zCPU,
        DLP/$, vbw, PCIe, type, $/hr, geo; `cores` = ours/total; `type` = OD
        (on-demand) / spot (interruptible). ``format="json"`` for raw dicts.

        - ``gpu_name``: family-aware ("A100" matches A100 PCIE + SXM4) or an
          exact Vast model ("A100 SXM4").
        - ``vram_gb``: keep only offers with this exact per-GPU VRAM (e.g. 40).
        - ``cpu_name``: family-aware ("trpro" matches AMD Threadripper PRO
          7/5/3 WX) or a literal substring of the host CPU ("5955WX",
          "EPYC 7763"); matched case-insensitively.
        - ``order``: sort key; prefix ``-`` for descending. Keys include
          ``cpu_ghz``, ``dph``/``price``, ``vram_gb``, ``cpu_cores``,
          ``reliability``, ``dlperf``, ``dlperf_per_dollar`` (perf-per-$),
          ``zcpu`` (synthetic CP2K score), ``zgpu`` (synthetic QE/FP64 score),
          ``gpu_mem_bw``, ``pcie_bw`` (e.g. ``-zgpu`` = best GPU-DFT host first).
        - ``min_cuda``: keep only hosts whose ``cuda_max_good`` (highest CUDA
          toolkit they run well) is >= this — match it to the image you deploy.
        - ``min_reliability`` / ``max_dph`` / ``verified``: marketplace filters.
        - ``host_type``: ``"any"`` (**default** — both on-demand and spot,
          merged, so nothing is hidden), ``"on-demand"`` (runs until destroyed),
          or ``"spot"`` (interruptible/bid; ``$/hr`` shown = min_bid, the spot
          floor; can be preempted). Pass ``"on-demand"`` explicitly for OD-only.
        """
        filters: dict[str, object] = {"verified": verified}
        if gpu_name:
            filters["gpu_name"] = gpu_name
        if vram_gb:
            filters["vram_gb"] = vram_gb
        if cpu_name:
            filters["cpu_name"] = cpu_name
        if min_cuda > 0:
            filters["min_cuda"] = min_cuda
        if min_reliability > 0:
            filters["min_reliability"] = min_reliability
        if max_dph > 0:
            filters["max_dph"] = max_dph
        if order:
            filters["order"] = order
        if host_type:
            filters["host_type"] = host_type
        offers = hub.sea_search(sea, filters=filters, limit=limit)
        if format == "json":
            return [_offer_to_dict(o) for o in offers]
        return _format_offers_table(offers)

    @mcp.tool()
    def seas_search(
        limit_per_sea: int = 7,
        gpu_name: str = "",
        vram_gb: int = 0,
        cpu_name: str = "",
        min_cuda: float = 0.0,
        min_reliability: float = 0.0,
        max_dph: float = 0.0,
        verified: bool = True,
        order: str = "",
        host_type: str = "any",
        format: str = "table",
    ) -> str | list[dict[str, Any]]:
        """Search ALL marketplace seas at once with one filter (no `sea` arg).

        The cross-sea view: queries every marketplace sea (vast / verda / clore —
        not owned docker hosts), takes the top ``limit_per_sea`` (default 7) from
        each, merges, and sorts the whole set as a single sea would (by ``order``,
        else cheapest first). The result table prepends a **`sea`** column so each
        row's provider is clear. Same filters/columns as ``sea_search``, including
        ``host_type`` which defaults to ``"any"`` (both on-demand and spot, so
        nothing is hidden); pass ``"on-demand"`` or ``"spot"`` to narrow. Relay
        the table verbatim. Any sea that fails (no creds / unreachable) is reported
        in a trailing note rather than silently dropped. ``format="json"`` for raw
        dicts (each includes its ``sea``).
        """
        filters: dict[str, object] = {"verified": verified}
        if gpu_name:
            filters["gpu_name"] = gpu_name
        if vram_gb:
            filters["vram_gb"] = vram_gb
        if cpu_name:
            filters["cpu_name"] = cpu_name
        if min_cuda > 0:
            filters["min_cuda"] = min_cuda
        if min_reliability > 0:
            filters["min_reliability"] = min_reliability
        if max_dph > 0:
            filters["max_dph"] = max_dph
        if order:
            filters["order"] = order
        if host_type:
            filters["host_type"] = host_type
        offers, errors = hub.seas_search(filters=filters, limit_per_sea=limit_per_sea)
        offers = _sort_merged(offers, order)
        if format == "json":
            return [_offer_to_dict(o) for o in offers]
        table = _format_offers_table(offers, with_sea=True)
        if errors:
            note = "; ".join(f"{s}: {e}" for s, e in errors.items())
            table += f"\n\n(seas skipped — {note})"
        return table

    @mcp.tool()
    def sea_recommend(
        sea: str,
        workload: str,
        budget_per_hour: float = 0.0,
        min_hours: int = 0,
        rank_by: str = "",
        format: str = "table",
    ) -> str | list[dict[str, Any]]:
        """Top offers for a workload preset (e.g. dft_paper_grade, mlip).

        Applies the preset's hard gate (FP64 / VRAM / GHz / cores / RAM / disk /
        reliability — our DEADLY_MISTAKES bar) then ranks the survivors by host
        fitness: ``zgpu`` for GPU-FP64 DFT, ``dlperf`` for MLIP (FP32), per the
        preset. Override with ``rank_by`` (``zgpu`` / ``zcpu`` / ``dlperf`` /
        ``dlperf_per_dollar`` / ``price``) — e.g. ``zcpu`` for a CP2K run.
        Ready-to-display table by default; ``format="json"`` for raw dicts.
        """
        offers = hub.sea_recommend(
            sea,
            workload=workload,
            budget_per_hour=budget_per_hour if budget_per_hour > 0 else None,
            min_hours=min_hours if min_hours > 0 else None,
        )
        key = rank_by or PRESETS[workload].rank_by
        offers = _rank_offers(offers, key)
        if format == "json":
            return [_offer_to_dict(o) for o in offers]
        return _format_offers_table(offers)

    @mcp.tool()
    def sea_status(sea: str) -> dict[str, Any]:
        """Reachability + balance + burn rate for a sea."""
        return asdict(hub.sea_status(sea))

    @mcp.tool()
    def cost_summary(sea: str = "") -> dict[str, Any]:
        """Total cost / burn / balance. Pass sea='' for an aggregate."""
        breakdown = hub.cost_summary(sea=sea or None)
        return {
            "sea": breakdown.sea,
            "total_per_hour": breakdown.total_per_hour,
            "per_host": [list(t) for t in breakdown.per_host],
            "balance": breakdown.balance,
            "burn_rate_24h": breakdown.burn_rate_24h,
            "days_remaining_at_balance": breakdown.days_remaining_at_balance,
        }

    # ---- host lifecycle ----

    @mcp.tool()
    def host_create(
        sea: str,
        image: str,
        name: str = "",
        offer_id: str = "",
        disk_gb: int = 0,
        onstart: str = "",
    ) -> dict[str, Any]:
        """Provision a new host in `sea` running `image`. Returns its handle."""
        handle = hub.host_create(
            sea,
            image=image,
            name=name or None,
            offer_id=offer_id or None,
            disk_gb=disk_gb if disk_gb > 0 else None,
            onstart=onstart or None,
        )
        return _handle_to_dict(handle)

    @mcp.tool()
    def host_add(name: str, target: str) -> str:
        """Register an existing pre-baked Lotsman endpoint (no provisioning)."""
        hub.host_add(name, target)
        return f"Host {name!r} added (target={target})"

    @mcp.tool()
    def host_destroy(name: str, kill_running: bool = False) -> str:
        """Tear down a host.

        Sea-managed → `docker rm`/`vastai destroy`; manual → unregister only.
        """
        hub.host_destroy(name, kill_running=kill_running)
        return f"Host {name!r} destroyed"

    @mcp.tool()
    def host_stop(name: str) -> str:
        """Stop a sea-managed host (resumable via host_start)."""
        hub.host_stop(name)
        return f"Host {name!r} stopped"

    @mcp.tool()
    def host_start(name: str) -> str:
        """Start a previously stopped sea-managed host."""
        hub.host_start(name)
        return f"Host {name!r} started"

    @mcp.tool()
    def host_list(sea: str = "") -> list[str]:
        """All known hosts; pass sea=NAME to filter."""
        return hub.host_list(sea=sea or None)

    # ---- per-job RPCs (unchanged from M1) ----

    @mcp.tool()
    def run(host: str, script: str, name: str = "") -> dict[str, Any]:
        resp = hub.run(host=host, script=script, name=name)
        return {"job_id": resp.job_id, "state": int(resp.state)}

    @mcp.tool()
    def status(job_id: str) -> dict[str, Any]:
        resp = hub.status(job_id)
        return {
            "job_id": resp.job_id,
            "state": int(resp.state),
            "exit_code": resp.exit_code if resp.HasField("exit_code") else None,
            "started_at_unix_ms": (
                resp.started_at_unix_ms if resp.HasField("started_at_unix_ms") else None
            ),
            "finished_at_unix_ms": (
                resp.finished_at_unix_ms if resp.HasField("finished_at_unix_ms") else None
            ),
        }

    @mcp.tool()
    def kill(job_id: str, grace_sec: float = 10.0, force: bool = False) -> dict[str, Any]:
        resp = hub.kill(job_id, grace_sec=grace_sec, force=force)
        return {"killed": resp.killed, "state": int(resp.state)}

    @mcp.tool()
    def logs(
        job_id: str, tail_lines: int = 0, include_stderr: bool = False
    ) -> dict[str, Any]:
        resp = hub.logs(
            job_id,
            tail_lines=tail_lines if tail_lines > 0 else None,
            include_stderr=include_stderr,
        )
        return {
            "stdout": resp.stdout.decode("utf-8", errors="replace"),
            "stderr": resp.stderr.decode("utf-8", errors="replace"),
            "stdout_total_bytes": resp.stdout_total_bytes,
            "stderr_total_bytes": resp.stderr_total_bytes,
        }

    @mcp.tool()
    def whoami(host: str) -> dict[str, Any]:
        resp = hub.whoami(host)
        return {
            "lotsman_version": resp.lotsman_version,
            "tool": resp.tool,
            "tool_version": resp.tool_version,
            "image": resp.image,
            "image_tag": resp.image_tag,
            "default_omp": resp.default_omp,
            "default_npool": resp.default_npool,
            "mpirun_required": resp.mpirun_required,
            "known_pitfalls": list(resp.known_pitfalls),
        }

    # ---- filesystem ----

    @mcp.tool()
    def upload(
        host: str,
        path: str,
        content: str = "",
        content_b64: str = "",
        create_parents: bool = True,
        overwrite: bool = False,
        executable: bool = False,
    ) -> dict[str, Any]:
        """Write a text or base64-encoded file to a host."""
        if content_b64:
            payload = base64.b64decode(content_b64)
        else:
            payload = content.encode("utf-8")
        resp = hub.upload(
            host,
            path=path,
            content=payload,
            create_parents=create_parents,
            overwrite=overwrite,
            executable=executable,
        )
        return {
            "path": resp.path,
            "bytes_written": resp.bytes_written,
            "sha256": resp.sha256,
        }

    @mcp.tool()
    def mkdir(
        host: str,
        path: str,
        parents: bool = True,
        exist_ok: bool = True,
    ) -> dict[str, Any]:
        """Create a directory on a host."""
        resp = hub.mkdir(host, path=path, parents=parents, exist_ok=exist_ok)
        return {"path": resp.path}

    @mcp.tool()
    def ls(host: str, path: str) -> list[dict[str, Any]]:
        """List a directory on a host."""
        return [_dir_entry_to_dict(e) for e in hub.ls(host, path).entries]

    @mcp.tool()
    def stat(host: str, path: str) -> dict[str, Any]:
        """Return file metadata on a host."""
        return _stat_to_dict(hub.stat(host, path))

    @mcp.tool()
    def cat(host: str, path: str, max_bytes: int = 0) -> dict[str, Any]:
        """Read a file snapshot from a host."""
        resp = hub.cat(host, path, max_bytes=max_bytes if max_bytes > 0 else None)
        return {
            "path": resp.path,
            "content": resp.content.decode("utf-8", errors="replace"),
            "content_b64": base64.b64encode(resp.content).decode("ascii"),
            "total_bytes": resp.total_bytes,
            "truncated": resp.truncated,
        }

    @mcp.tool()
    def disk_free(host: str, path: str = ".") -> dict[str, Any]:
        """Return filesystem capacity for a path on a host."""
        resp = hub.disk_free(host, path)
        return {
            "path": resp.path,
            "total_bytes": resp.total_bytes,
            "used_bytes": resp.used_bytes,
            "free_bytes": resp.free_bytes,
        }

    # ---- harvest / download ----

    @mcp.tool()
    def harvest_inventory(job_id: str, mode: str = "essential") -> dict[str, Any]:
        """Preview which files a job harvest would include."""
        resp = hub.harvest_inventory(job_id, mode=mode)
        return {
            "job_id": resp.job_id,
            "mode": resp.mode,
            "included_bytes": resp.included_bytes,
            "entries": [_harvest_entry_to_dict(e) for e in resp.entries],
        }

    @mcp.tool()
    def harvest(
        job_id: str,
        mode: str = "essential",
        format: str = "tar.gz",
        include_content: bool = False,
    ) -> dict[str, Any]:
        """Create a guarded job harvest archive."""
        resp = hub.harvest(job_id, mode=mode, format=format)
        out: dict[str, Any] = {
            "job_id": resp.job_id,
            "mode": resp.mode,
            "format": resp.format,
            "archive_path": resp.archive_path,
            "archive_bytes": resp.archive_bytes,
            "sha256": resp.sha256,
            "entries": [_harvest_entry_to_dict(e) for e in resp.entries],
        }
        if include_content:
            host = resp.job_id.split("/", 1)[0]
            content = hub.download(host, resp.archive_path).content
            out["archive_b64"] = base64.b64encode(content).decode("ascii")
        return out

    @mcp.tool()
    def download(host: str, path: str, max_bytes: int = 0) -> dict[str, Any]:
        """Download one file snapshot from a host."""
        resp = hub.download(host, path, max_bytes=max_bytes if max_bytes > 0 else None)
        return {
            "path": resp.path,
            "content_b64": base64.b64encode(resp.content).decode("ascii"),
            "total_bytes": resp.total_bytes,
            "truncated": resp.truncated,
        }

    @mcp.tool()
    def download_glob(
        host: str,
        pattern: str,
        format: str = "tar.gz",
        confirm_size_gb: float = 0.0,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """Create a guarded archive from a glob expression."""
        resp = hub.download_glob(
            host,
            pattern=pattern,
            format=format,
            confirm_size_gb=confirm_size_gb,
        )
        out: dict[str, Any] = {
            "pattern": resp.pattern,
            "format": resp.format,
            "archive_path": resp.archive_path,
            "archive_bytes": resp.archive_bytes,
            "sha256": resp.sha256,
            "entries": [_harvest_entry_to_dict(e) for e in resp.entries],
        }
        if include_content:
            content = hub.download(host, resp.archive_path).content
            out["archive_b64"] = base64.b64encode(content).decode("ascii")
        return out

    # ---- watchdogs / events ----

    @mcp.tool()
    def watchdog_list(job_id: str) -> list[dict[str, Any]]:
        """Active watchdogs for a job (defaults from manifest + extras)."""
        resp = hub.watchdog_list(job_id)
        return [_watchdog_to_dict(w) for w in resp.watchdogs]

    @mcp.tool()
    def watchdog_history(
        job_id: str, since_unix_ms: int = 0
    ) -> list[dict[str, Any]]:
        """Past fired events for a job. since_unix_ms=0 returns all history."""
        resp = hub.watchdog_history(job_id, since_unix_ms=since_unix_ms)
        return [_event_to_dict(e) for e in resp.events]

    @mcp.tool()
    def events(job_id: str, since_unix_ms: int = 0) -> list[dict[str, Any]]:
        """Snapshot of events for a job (alias of watchdog_history)."""
        resp = hub.watchdog_history(job_id, since_unix_ms=since_unix_ms)
        return [_event_to_dict(e) for e in resp.events]

    @mcp.tool()
    def events_all(
        since_unix_ms: int = 0, hosts: str = ""
    ) -> dict[str, list[dict[str, Any]]]:
        """Aggregate events across the fleet.

        Pass hosts as a comma-separated list to filter; default = all hosts.
        Returns {host_name: [event_dict, ...]}.
        """
        host_filter = (
            [h.strip() for h in hosts.split(",") if h.strip()] if hosts else None
        )
        per_host = hub.events_all(since_unix_ms=since_unix_ms, hosts=host_filter)
        return {h: [_event_to_dict(e) for e in events] for h, events in per_host.items()}

    return mcp


# ---- helpers ----


# Ranking keys for sea_recommend (Offer attribute, descending = better) — plus
# "price" which sorts ascending. Used to order offers that pass a preset gate.
_RANK_ATTR = {
    "zgpu": "zgpu", "zcpu": "zcpu", "dlperf": "dlperf",
    "dlperf_per_dollar": "dlperf_per_dollar", "dlpd": "dlperf_per_dollar",
}


def _rank_offers(offers: list[Any], key: str) -> list[Any]:
    """Sort offers by a fitness key (descending), or by price ascending."""
    if key in ("price", "dph", "price_per_hour"):
        return sorted(offers, key=lambda o: o.price_per_hour)
    attr = _RANK_ATTR.get(key, "zgpu")
    return sorted(offers, key=lambda o: getattr(o, attr), reverse=True)


def _short_cpu(name: str) -> str:
    """Compact CPU label for the results table (drop brand noise + core count)."""
    s = name or ""
    for junk in ("(R)", "®", "™", "Processor", "CPU"):
        s = s.replace(junk, "")
    s = s.replace("Threadripper PRO", "TR PRO").replace("Threadripper", "TR")
    s = s.replace("AMD Ryzen ", "").replace("AMD ", "")
    s = re.sub(r"\s*\d+[- ]Cores?\b", "", s)  # drop "64-Core" / "16-Cores"
    s = re.sub(r"\s+", " ", s).strip()
    return s[:22]


# Fixed result columns (label, right-aligned?, value-fn). ID always first so the
# user can name a contract to buy. Rendered server-side to save agent tokens.
_TABLE_COLUMNS: tuple[tuple[str, bool, Any], ...] = (
    ("ID", False, lambda o: o.offer_id),
    ("GPU", False, lambda o: f"{o.gpu_count}x{o.gpu_model}"),
    ("VRAM", True, lambda o: f"{o.vram_gb}G"),
    ("CUDA", True, lambda o: f"{o.cuda_max_good:.1f}"),
    ("CPU", False, lambda o: _short_cpu(o.cpu_name)),
    ("cores", True, lambda o: f"{o.cpu_cores}/{o.cpu_cores_total}"),
    ("RAM", True, lambda o: f"{o.ram_gb}G"),
    ("Disk", True, lambda o: f"{o.disk_gb}G"),
    ("zGPU", True, lambda o: str(o.zgpu)),
    ("zCPU", True, lambda o: str(o.zcpu)),
    ("DLP/$", True, lambda o: f"{o.dlperf_per_dollar:.0f}"),
    ("vbw", True, lambda o: f"{o.gpu_mem_bw_gbs:.0f}"),
    ("PCIe", True, lambda o: f"{o.pcie_bw_gbs:.1f}"),
    # Host type, third from the right (before price): OD = on-demand (runs until
    # destroyed), spot = interruptible/bid (preemptible; $/hr = min_bid floor).
    ("type", True, lambda o: "spot" if o.host_type == "interruptible" else "OD"),
    ("$/hr", True, lambda o: f"{o.price_per_hour:.2f}"),
    ("geo", False, lambda o: (o.geolocation or "")[:20]),
)


# Leading column for the cross-sea (seas_search) view: which sea an offer is from.
_SEA_COLUMN: tuple[str, bool, Any] = ("sea", False, lambda o: o.sea)


def _format_offers_table(offers: list[Any], with_sea: bool = False) -> str:
    """Render offers as a fixed-column aligned text table (ready to display).

    ``with_sea`` prepends a ``sea`` column (used by the cross-sea seas_search).
    """
    if not offers:
        return "(no offers)"
    columns = ((_SEA_COLUMN, *_TABLE_COLUMNS) if with_sea else _TABLE_COLUMNS)
    headers = [c[0] for c in columns]
    rows = [[str(fn(o)) for _, _, fn in columns] for o in offers]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows))
        for i in range(len(headers))
    ]

    def fmt(cells: list[str]) -> str:
        out = []
        for i, (_, right, _) in enumerate(columns):
            out.append(cells[i].rjust(widths[i]) if right else cells[i].ljust(widths[i]))
        return " | ".join(out)

    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt(headers), sep, *(fmt(r) for r in rows)])


# Sort keys for the merged cross-sea result (Offer attributes common to all seas).
_MERGED_SORT_KEYS = {
    "price": "price_per_hour", "dph": "price_per_hour",
    "price_per_hour": "price_per_hour",
    "cpu_ghz": "cpu_ghz", "ghz": "cpu_ghz",
    "vram": "vram_gb", "vram_gb": "vram_gb",
    "cores": "cpu_cores", "cpu_cores": "cpu_cores",
    "ram": "ram_gb", "ram_gb": "ram_gb",
    "disk": "disk_gb", "disk_gb": "disk_gb",
    "reliability": "reliability",
    "dlperf": "dlperf",
    "dlpd": "dlperf_per_dollar", "dlperf_per_dollar": "dlperf_per_dollar",
    "zcpu": "zcpu", "zgpu": "zgpu",
    "gpu_mem_bw": "gpu_mem_bw_gbs", "pcie_bw": "pcie_bw_gbs",
}


def _sort_merged(offers: list[Any], order: str) -> list[Any]:
    """Sort merged cross-sea offers; '' = cheapest first, '-' prefix = descending."""
    if not order:
        return sorted(offers, key=lambda o: o.price_per_hour)
    desc = order.startswith("-")
    attr = _MERGED_SORT_KEYS.get((order[1:] if desc else order).lower())
    if attr is None:
        raise ValueError(
            f"unknown sort key {order!r}; known: {sorted(set(_MERGED_SORT_KEYS))}"
        )

    def keyfn(o: Any) -> float:
        val = getattr(o, attr, None)
        return float(val) if val is not None else float("-inf")

    return sorted(offers, key=keyfn, reverse=desc)


def _offer_to_dict(o: Any) -> dict[str, Any]:
    return {
        "sea": o.sea,
        "offer_id": o.offer_id,
        "gpu_model": o.gpu_model,
        "gpu_count": o.gpu_count,
        "vram_gb": o.vram_gb,
        "fp64_native": o.fp64_native,
        "cpu_name": o.cpu_name,
        "cpu_ghz": o.cpu_ghz,
        "cpu_cores": o.cpu_cores,            # cores rented to us
        "cpu_cores_total": o.cpu_cores_total,  # total cores on the host
        "ram_gb": o.ram_gb,
        "disk_gb": o.disk_gb,
        "price_per_hour": o.price_per_hour,
        "host_type": o.host_type,  # "on-demand" | "interruptible" (spot)
        "inet_down_mbps": o.inet_down_mbps,
        "geolocation": o.geolocation,
        "dlperf": o.dlperf,                    # raw DL perf score
        "dlperf_per_dollar": o.dlperf_per_dollar,  # perf per $/hr (bang-for-buck)
        "gpu_mem_bw_gbs": o.gpu_mem_bw_gbs,    # measured VRAM bandwidth
        "pcie_bw_gbs": o.pcie_bw_gbs,          # measured host<->GPU PCIe bandwidth
        "zcpu": o.zcpu,                        # synthetic CPU-DFT (CP2K) score
        "zgpu": o.zgpu,                        # synthetic GPU-DFT (QE) score
    }


def _handle_to_dict(h: Any) -> dict[str, Any]:
    return {
        "name": h.name,
        "sea": h.sea,
        "instance_id": h.instance_id,
        "grpc_target": h.grpc_target,
        "state": h.state,
        "cost_per_hour": h.cost_per_hour,
        "created_at_unix_ms": h.created_at_unix_ms,
        "ssh_target": h.ssh_target,
    }


def _watchdog_to_dict(w: Any) -> dict[str, Any]:
    return {
        "name": w.name,
        "fired": w.fired,
        "action": w.action,
        "interval_sec": w.interval_sec,
    }


def _event_to_dict(e: Any) -> dict[str, Any]:
    return {
        "job_id": e.job_id,
        "watchdog_name": e.watchdog_name,
        "event_type": e.event_type,
        "unix_ms": e.unix_ms,
        "detail": e.detail,
        "severity": e.severity,
        "data": dict(e.data),
    }


def _dir_entry_to_dict(e: Any) -> dict[str, Any]:
    return {
        "name": e.name,
        "path": e.path,
        "is_dir": e.is_dir,
        "size_bytes": e.size_bytes,
        "mtime_unix_ms": e.mtime_unix_ms,
    }


def _stat_to_dict(s: Any) -> dict[str, Any]:
    return {
        "path": s.path,
        "exists": s.exists,
        "is_dir": s.is_dir,
        "size_bytes": s.size_bytes,
        "mtime_unix_ms": s.mtime_unix_ms,
    }


def _harvest_entry_to_dict(e: Any) -> dict[str, Any]:
    return {
        "path": e.path,
        "size_bytes": e.size_bytes,
        "included": e.included,
        "reason": e.reason,
    }
