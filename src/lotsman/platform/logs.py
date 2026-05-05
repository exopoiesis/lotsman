from __future__ import annotations


def tail_bytes(data: bytes, n_lines: int) -> bytes:
    if n_lines <= 0:
        return b""
    lines = data.splitlines(keepends=True)
    return b"".join(lines[-n_lines:])
