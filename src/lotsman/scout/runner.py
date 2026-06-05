from __future__ import annotations

import json
import os
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lotsman.scout.commands import CommandResult, run_command, which_any

CommandRunner = Callable[[str, list[str], float, dict[str, str] | None], CommandResult]


@dataclass(frozen=True)
class ScoutConfig:
    workspace: Path = Path("/workspace")
    fio_size_mb: int = 1024
    fio_runtime_s: int = 10
    command_timeout_s: float = 120.0
    dmon_samples: int = 10
    run_fio: bool = True
    run_dmon: bool = True
    run_nvbandwidth: bool = True
    run_dcgm: bool = False
    run_stream: bool = True


def run_scout(
    config: ScoutConfig,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, object]:
    runner = command_runner or _run_command_adapter
    started = time.time()
    workspace = config.workspace
    workspace.mkdir(parents=True, exist_ok=True)

    tools = _tool_inventory()
    effective_cores = _effective_cores()
    result: dict[str, object] = {
        "schema_version": 1,
        "started_at_unix_ms": int(started * 1000),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "effective_cores": effective_cores,
        },
        "tools": tools,
        "inventory": _run_inventory(runner, config, workspace),
        "probes": {},
    }

    probes: dict[str, object] = {}
    if config.run_fio:
        probes["fio"] = _run_fio(runner, config, workspace, tools)
    if config.run_stream:
        probes["stream"] = _run_stream(runner, config, effective_cores, tools)
    if config.run_dmon:
        probes["nvidia_smi_dmon"] = _run_nvidia_dmon(runner, config, tools)
    if config.run_nvbandwidth:
        probes["nvbandwidth"] = _run_nvbandwidth(runner, config, tools)
    if config.run_dcgm:
        probes["dcgm"] = _run_dcgm(runner, config, tools)

    result["probes"] = probes
    result["duration_ms"] = int((time.time() - started) * 1000)
    return result


def _run_command_adapter(
    name: str,
    argv: list[str],
    timeout_s: float,
    env: dict[str, str] | None,
) -> CommandResult:
    return run_command(name, argv, timeout_s=timeout_s, env=env)


def _tool_inventory() -> dict[str, object]:
    stream = which_any(["stream", "stream_c", "stream.omp"])
    return {
        "fio": which_any(["fio"]),
        "stream": stream,
        "nvidia_smi": which_any(["nvidia-smi"]),
        "nvbandwidth": which_any(["nvbandwidth"]),
        "dcgmi": which_any(["dcgmi"]),
        "nccl_all_reduce_perf": which_any(["all_reduce_perf"]),
    }


def _run_inventory(
    runner: CommandRunner,
    config: ScoutConfig,
    workspace: Path,
) -> dict[str, object]:
    commands = [
        ("uname", ["uname", "-a"]),
        ("lscpu", ["lscpu"]),
        ("free", ["free", "-b"]),
        ("df_workspace", ["df", "-B1", str(workspace)]),
    ]
    inventory: dict[str, object] = {
        "cpu_max": _read_cgroup_cpu_max(),
        "meminfo": _read_text(Path("/proc/meminfo")),
        "commands": [
            runner(name, argv, config.command_timeout_s, None).to_dict()
            for name, argv in commands
        ],
    }

    if which_any(["nvidia-smi"]):
        inventory["nvidia_smi_query"] = runner(
            "nvidia_smi_query",
            [
                "nvidia-smi",
                "--query-gpu=index,name,uuid,memory.total,pcie.link.gen.current,"
                "pcie.link.width.current,power.limit,clocks.max.sm,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            config.command_timeout_s,
            None,
        ).to_dict()
        inventory["nvidia_smi_topo"] = runner(
            "nvidia_smi_topo",
            ["nvidia-smi", "topo", "-m"],
            config.command_timeout_s,
            None,
        ).to_dict()

    return inventory


def _run_fio(
    runner: CommandRunner,
    config: ScoutConfig,
    workspace: Path,
    tools: dict[str, object],
) -> dict[str, object]:
    if not tools["fio"]:
        return {"status": "skipped", "reason": "fio not found"}

    filename = workspace / "lotsman_scout_fio.bin"
    size = f"{config.fio_size_mb}M"
    common = [
        "fio",
        f"--filename={filename}",
        f"--size={size}",
        "--direct=1",
        "--ioengine=libaio",
        "--iodepth=16",
        "--numjobs=1",
        f"--runtime={config.fio_runtime_s}",
        "--time_based",
        "--group_reporting",
        "--output-format=json",
    ]
    jobs = [
        ("seq_write", ["--name=scout_seq_write", "--rw=write", "--bs=1M"]),
        ("seq_read", ["--name=scout_seq_read", "--rw=read", "--bs=1M"]),
        (
            "randrw_4k",
            ["--name=scout_randrw_4k", "--rw=randrw", "--rwmixread=70", "--bs=4k"],
        ),
    ]
    results: list[dict[str, object]] = []
    for name, extra in jobs:
        cmd = runner(
            f"fio_{name}",
            [*common, *extra],
            max(config.command_timeout_s, config.fio_runtime_s + 30),
            None,
        )
        results.append(_with_json_summary(cmd))

    try:
        filename.unlink(missing_ok=True)
    except OSError as exc:
        return {"status": "cleanup_failed", "commands": results, "cleanup_error": str(exc)}

    return {"status": "ok", "commands": results}


def _run_stream(
    runner: CommandRunner,
    config: ScoutConfig,
    effective_cores: float | None,
    tools: dict[str, object],
) -> dict[str, object]:
    stream = tools["stream"]
    if not isinstance(stream, str) or not stream:
        return {"status": "skipped", "reason": "stream binary not found"}

    threads = (
        int(effective_cores)
        if effective_cores and effective_cores >= 1
        else os.cpu_count() or 1
    )
    env = dict(os.environ)
    env["OMP_NUM_THREADS"] = str(max(1, threads))
    return {
        "status": "ok",
        "command": runner(
            "stream",
            [stream],
            config.command_timeout_s,
            env,
        ).to_dict(),
        "omp_num_threads": env["OMP_NUM_THREADS"],
    }


def _run_nvidia_dmon(
    runner: CommandRunner,
    config: ScoutConfig,
    tools: dict[str, object],
) -> dict[str, object]:
    if not tools["nvidia_smi"]:
        return {"status": "skipped", "reason": "nvidia-smi not found"}
    return runner(
        "nvidia_smi_dmon",
        ["nvidia-smi", "dmon", "-s", "pucvmt", "-d", "1", "-c", str(config.dmon_samples)],
        max(config.command_timeout_s, config.dmon_samples + 10),
        None,
    ).to_dict()


def _run_nvbandwidth(
    runner: CommandRunner,
    config: ScoutConfig,
    tools: dict[str, object],
) -> dict[str, object]:
    binary = tools["nvbandwidth"]
    if not isinstance(binary, str) or not binary:
        return {"status": "skipped", "reason": "nvbandwidth not found"}
    return runner("nvbandwidth", [binary], config.command_timeout_s, None).to_dict()


def _run_dcgm(
    runner: CommandRunner,
    config: ScoutConfig,
    tools: dict[str, object],
) -> dict[str, object]:
    if not tools["dcgmi"]:
        return {"status": "skipped", "reason": "dcgmi not found"}

    commands = [
        ("dcgm_pcie", ["dcgmi", "diag", "-r", "pcie", "-j"]),
        (
            "dcgm_memory_bandwidth",
            [
                "dcgmi",
                "diag",
                "-r",
                "memory_bandwidth",
                "-p",
                "memory_bandwidth.is_allowed=True",
                "-j",
            ],
        ),
        (
            "dcgm_targeted_stress",
            [
                "dcgmi",
                "diag",
                "-r",
                "targeted_stress",
                "-p",
                "targeted_stress.test_duration=30.0",
                "-j",
            ],
        ),
    ]
    return {
        "status": "ok",
        "commands": [
            _with_json_summary(runner(name, argv, config.command_timeout_s, None))
            for name, argv in commands
        ],
    }


def _with_json_summary(result: CommandResult) -> dict[str, object]:
    data = result.to_dict()
    try:
        data["json"] = json.loads(result.stdout)
    except json.JSONDecodeError:
        pass
    return data


def _effective_cores() -> float | None:
    raw = _read_cgroup_cpu_max()
    quota = raw.get("quota_us")
    period = raw.get("period_us")
    if isinstance(quota, int) and isinstance(period, int) and quota > 0 and period > 0:
        return quota / period
    return None


def _read_cgroup_cpu_max() -> dict[str, object]:
    v2 = Path("/sys/fs/cgroup/cpu.max")
    if v2.exists():
        raw = _read_text(v2).strip()
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            return {
                "version": 2,
                "raw": raw,
                "quota_us": _parse_int(parts[0]),
                "period_us": _parse_int(parts[1]),
            }
        return {"version": 2, "raw": raw, "quota_us": None, "period_us": None}

    quota_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota_path.exists() and period_path.exists():
        quota = _parse_int(_read_text(quota_path).strip())
        period = _parse_int(_read_text(period_path).strip())
        return {
            "version": 1,
            "raw": f"{quota} {period}",
            "quota_us": quota if quota and quota > 0 else None,
            "period_us": period,
        }
    return {"version": 0, "raw": "", "quota_us": None, "period_us": None}


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
