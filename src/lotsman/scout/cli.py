from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lotsman.scout.runner import ScoutConfig, run_scout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lotsman scout")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run hardware probes and emit JSON")
    p_run.add_argument("--workspace", default="/workspace")
    p_run.add_argument("--out", help="Output JSON path; stdout if omitted")
    p_run.add_argument("--fio-size-mb", type=int, default=1024)
    p_run.add_argument("--fio-runtime-s", type=int, default=10)
    p_run.add_argument("--timeout-s", type=float, default=120.0)
    p_run.add_argument("--dmon-samples", type=int, default=10)
    p_run.add_argument("--dcgm", action="store_true", help="Run optional DCGM diagnostics")
    p_run.add_argument("--no-fio", action="store_true")
    p_run.add_argument("--no-stream", action="store_true")
    p_run.add_argument("--no-dmon", action="store_true")
    p_run.add_argument("--no-nvbandwidth", action="store_true")
    p_run.add_argument("--pretty", action="store_true")
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


def _cmd_run(args: argparse.Namespace) -> int:
    config = ScoutConfig(
        workspace=Path(args.workspace),
        fio_size_mb=args.fio_size_mb,
        fio_runtime_s=args.fio_runtime_s,
        command_timeout_s=args.timeout_s,
        dmon_samples=args.dmon_samples,
        run_fio=not args.no_fio,
        run_stream=not args.no_stream,
        run_dmon=not args.no_dmon,
        run_nvbandwidth=not args.no_nvbandwidth,
        run_dcgm=args.dcgm,
    )
    result = run_scout(config)
    payload = json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
