"""Minimal `.env` loader (no third-party dependency).

Marina secrets (e.g. ``VAST_API_KEY``) live in a gitignored ``.env`` rather
than in ``marina.toml`` plaintext or the MCP launcher config. We parse
``KEY=VALUE`` lines ourselves to avoid pulling ``python-dotenv`` into the
runtime dependency set.

Precedence: an already-exported process environment variable always wins over
a ``.env`` entry (so a real shell export can override the file), and an earlier
``.env`` file wins over a later one for the same key.
"""
from __future__ import annotations

import os
from pathlib import Path


def find_env_files(config_path: Path | None) -> list[Path]:
    """Candidate ``.env`` locations, in priority order.

    The file next to the config (``~/.lotsman/.env``) is the conventional home;
    a ``.env`` in the current working directory is also honoured for dev.
    """
    candidates: list[Path] = []
    if config_path is not None:
        candidates.append(config_path.parent / ".env")
    candidates.append(Path.cwd() / ".env")
    out: list[Path] = []
    seen: set[Path] = set()
    for c in candidates:
        try:
            key = c.resolve()
        except OSError:
            key = c
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ignore blanks, ``#`` comments, ``export``."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def load_dotenv(config_path: Path | None = None) -> list[str]:
    """Load ``.env`` files into ``os.environ`` without overriding existing vars.

    Returns the list of keys that were newly set, for logging — never values.
    """
    loaded: list[str] = []
    for path in find_env_files(config_path):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for key, value in parse_env(text).items():
            if key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded
