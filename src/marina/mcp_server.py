from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from marina.hub import Hub


def make_mcp_server(hub: Hub, name: str = "Marina") -> FastMCP:
    mcp = FastMCP(name)

    @mcp.tool()
    def host_add(name: str, target: str) -> str:
        hub.host_add(name, target)
        return f"Host {name!r} added (target={target})"

    @mcp.tool()
    def host_remove(name: str) -> str:
        hub.host_remove(name)
        return f"Host {name!r} removed"

    @mcp.tool()
    def host_list() -> list[str]:
        return hub.host_list()

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
