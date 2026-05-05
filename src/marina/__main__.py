from __future__ import annotations

import argparse
import sys
from pathlib import Path

from marina.config import load_config
from marina.hub import Hub
from marina.mcp_server import make_mcp_server


def cmd_serve(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else None
    cfg = load_config(config_path)

    hub = Hub()
    for host in cfg.hosts:
        hub.host_add(host.name, host.target)
        print(f"Marina: registered host {host.name!r} -> {host.target}", file=sys.stderr, flush=True)

    server = make_mcp_server(hub, name="Marina")
    print(f"Marina: serving MCP over stdio ({len(cfg.hosts)} host(s) loaded)", file=sys.stderr, flush=True)

    try:
        server.run()
    finally:
        hub.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marina", description="Marina local hub for Lotsman fleet")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Run MCP server over stdio")
    p_serve.add_argument(
        "--config",
        help="path to marina.toml (~/.lotsman/marina.toml is conventional)",
    )
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
