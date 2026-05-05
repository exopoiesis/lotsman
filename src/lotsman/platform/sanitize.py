from __future__ import annotations


def sanitize_script(script: str) -> str:
    return script.replace("\r\n", "\n").replace("—", "--")
