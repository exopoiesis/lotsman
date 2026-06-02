from __future__ import annotations

import argparse
import os
import signal
import sys
from concurrent import futures
from pathlib import Path
from types import FrameType

import grpc

from lotsman.server import LotsmanService
from lotsman.v1 import lotsman_pb2_grpc
from lotsman.watchdogs import Check, DiskLowCheck, GpuIdleCheck, ProcessExitOomCheck


def _watchdog_defaults_from_env() -> list[Check]:
    """Build the production default watchdog set, with env-var overrides.

    Env vars:
      LOTSMAN_DISK_LOW_GB         (default 5.0)
      LOTSMAN_DISK_LOW_INTERVAL_S (default 60)
      LOTSMAN_GPU_IDLE_PCT        (default 5.0)
      LOTSMAN_GPU_IDLE_SECONDS    (default 1800)
      LOTSMAN_GPU_IDLE_INTERVAL_S (default 60)
    """
    disk_low_gb = float(os.environ.get("LOTSMAN_DISK_LOW_GB", "5.0"))
    disk_low_interval = float(os.environ.get("LOTSMAN_DISK_LOW_INTERVAL_S", "60"))
    gpu_idle_pct = float(os.environ.get("LOTSMAN_GPU_IDLE_PCT", "5.0"))
    gpu_idle_seconds = float(os.environ.get("LOTSMAN_GPU_IDLE_SECONDS", "1800"))
    gpu_idle_interval = float(os.environ.get("LOTSMAN_GPU_IDLE_INTERVAL_S", "60"))
    return [
        DiskLowCheck(threshold_gb=disk_low_gb, interval_sec=disk_low_interval),
        ProcessExitOomCheck(),
        GpuIdleCheck(
            threshold_pct=gpu_idle_pct,
            idle_seconds=gpu_idle_seconds,
            interval_sec=gpu_idle_interval,
        ),
    ]


def cmd_serve(args: argparse.Namespace) -> int:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=args.workers))
    servicer = LotsmanService(
        host_id=args.host_id,
        jobs_dir=Path(args.jobs_dir),
        manifest_path=Path(args.manifest) if args.manifest else None,
        default_checks=_watchdog_defaults_from_env(),
    )
    lotsman_pb2_grpc.add_LotsmanServiceServicer_to_server(servicer, server)

    if args.unix_socket:
        bind = f"unix://{args.unix_socket}"
    else:
        bind = f"[::]:{args.port}"
    server.add_insecure_port(bind)
    server.start()

    print(f"Lotsman serving on {bind} (host_id={args.host_id})", flush=True)

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        servicer.shutdown()
        server.stop(grace=2.0)

    signal.signal(signal.SIGTERM, _shutdown)
    try:
        signal.signal(signal.SIGINT, _shutdown)
    except ValueError:
        pass  # not in main thread (tests)

    server.wait_for_termination()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lotsman", description="Lotsman in-container daemon")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Run the gRPC server")
    p_serve.add_argument("--host-id", default="local", help="self-identifier in jobIds")
    p_serve.add_argument("--port", type=int, default=50051)
    p_serve.add_argument("--unix-socket", help="UDS path; if set, --port is ignored")
    p_serve.add_argument("--jobs-dir", default="/var/lotsman/jobs")
    p_serve.add_argument("--manifest", help="path to /etc/lotsman/manifest.toml")
    p_serve.add_argument("--workers", type=int, default=8)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
