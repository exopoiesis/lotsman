from __future__ import annotations

import shutil


def resolve_bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError("bash not found in PATH; required to run scripts")
    return bash
