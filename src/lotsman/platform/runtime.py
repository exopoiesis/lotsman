from __future__ import annotations

import platform
import shutil
from pathlib import Path

WINDOWS_GIT_BASH_CANDIDATES = (
    Path(r"C:\Program Files\Git\bin\bash.exe"),
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
)


def resolve_bash() -> str:
    if platform.system() == "Windows":
        for candidate in WINDOWS_GIT_BASH_CANDIDATES:
            if candidate.exists():
                return str(candidate)

    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError("bash not found in PATH; required to run scripts")
    if platform.system() == "Windows" and "system32" in bash.lower():
        raise RuntimeError(
            "Windows WSL bash shim found, but Git Bash is required to run scripts"
        )
    return bash
