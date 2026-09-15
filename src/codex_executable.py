#!/usr/bin/env python3
"""Resolve a cross-platform command for the Codex CLI."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resolve_codex_command() -> list[str]:
    configured = os.environ.get("CODEX_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured)
        return [str(path)]

    candidates = (
        ["codex.cmd", "codex.exe", "codex"]
        if sys.platform == "win32"
        else ["codex"]
    )
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return [resolved]
    return ["codex"]
