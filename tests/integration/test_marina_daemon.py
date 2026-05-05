from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_marina_daemon_help_works():
    """Smoke: marina --help exits 0 (validates argparse + module import)."""
    result = subprocess.run(
        [sys.executable, "-m", "marina", "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "marina" in result.stdout.lower()
    assert "serve" in result.stdout.lower()


def test_marina_daemon_serve_help_works():
    result = subprocess.run(
        [sys.executable, "-m", "marina", "serve", "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "--config" in result.stdout


def test_marina_daemon_loads_config_no_hosts(tmp_path: Path):
    """Marina starts even with no hosts in config; serves over stdio."""
    config = tmp_path / "marina.toml"
    config.write_text("# empty\n", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "marina", "serve", "--config", str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Give it a moment to start, then close stdin -> EOF -> exit
        import time

        time.sleep(0.5)
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=5)
        stderr = proc.stderr.read() if proc.stderr else b""
        assert b"serving MCP over stdio" in stderr or b"0 host" in stderr
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
