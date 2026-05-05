from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP

from marina.hub import Hub


def make_mcp_server(hub: Hub, name: str = "Marina") -> FastMCP:
    mcp = FastMCP(name)

    # ---- sea registry / queries ----

    @mcp.tool()
    def sea_list() -> list[str]:
        """All seas (hosting providers) registered with Marina."""
        return hub.sea_list()

    @mcp.tool()
    def sea_search(sea: str, limit: int = 20) -> list[dict[str, Any]]:
        """List offers in the given sea (free-form filters TBD)."""
        return [_offer_to_dict(o) for o in hub.sea_search(sea, limit=limit)]

    @mcp.tool()
    def sea_recommend(
        sea: str,
        workload: str,
        budget_per_hour: float = 0.0,
        min_hours: int = 0,
    ) -> list[dict[str, Any]]:
        """Top offers for a workload preset (e.g. dft_paper_grade, mlip)."""
        offers = hub.sea_recommend(
            sea,
            workload=workload,
            budget_per_hour=budget_per_hour if budget_per_hour > 0 else None,
            min_hours=min_hours if min_hours > 0 else None,
        )
        return [_offer_to_dict(o) for o in offers]

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
    def run(host: str, script: str, name: str = "") -> dict:
        resp = hub.run(host=host, script=script, name=name)
        return {"job_id": resp.job_id, "state": int(resp.state)}

    @mcp.tool()
    def status(job_id: str) -> dict:
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
    def kill(job_id: str, grace_sec: float = 10.0, force: bool = False) -> dict:
        resp = hub.kill(job_id, grace_sec=grace_sec, force=force)
        return {"killed": resp.killed, "state": int(resp.state)}

    @mcp.tool()
    def logs(
        job_id: str, tail_lines: int = 0, include_stderr: bool = False
    ) -> dict:
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
    def whoami(host: str) -> dict:
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

    return mcp


# ---- helpers ----


def _offer_to_dict(o: Any) -> dict[str, Any]:
    return {
        "sea": o.sea,
        "offer_id": o.offer_id,
        "gpu_model": o.gpu_model,
        "gpu_count": o.gpu_count,
        "vram_gb": o.vram_gb,
        "fp64_native": o.fp64_native,
        "cpu_ghz": o.cpu_ghz,
        "cpu_cores": o.cpu_cores,
        "ram_gb": o.ram_gb,
        "disk_gb": o.disk_gb,
        "price_per_hour": o.price_per_hour,
        "reliability": o.reliability,
        "inet_down_mbps": o.inet_down_mbps,
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
