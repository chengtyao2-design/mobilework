"""Terminal styling shared by every human-facing entry point.

Decorative output must never reach a machine-readable channel: agents parse
`wiki` stdout as JSON, and the retrieval eval harness parses
`wiki_retrieval.index --status` stderr as JSON. So styling is gated on stderr
being an interactive terminal — under a pipe nothing extra is emitted at all.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

STATUS_COLORS = {
    "ok": GREEN,
    "committed": GREEN,
    "prepared": GREEN,
    "initialized": GREEN,
    "warn": YELLOW,
    "unchanged": DIM,
    "skipped": DIM,
    "fail": RED,
    "failed": RED,
    "error": RED,
    "agent_failed_or_incomplete": RED,
}


def enabled(stream: TextIO | None = None) -> bool:
    """True only for a real terminal that has not opted out of colour."""
    target = stream if stream is not None else sys.stderr
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


def paint(text: str, color: str, stream: TextIO | None = None) -> str:
    if not color or not enabled(stream):
        return text
    return f"{color}{text}{RESET}"


def status_color(state: str) -> str:
    return STATUS_COLORS.get(state, "")


def note(text: str, color: str = "", stream: TextIO | None = None) -> None:
    """Write one decorative line, or nothing when styling is disabled."""
    target = stream if stream is not None else sys.stderr
    if not enabled(target):
        return
    print(paint(text, color, target), file=target, flush=True)
