"""Terminal styling shared by every human-facing entry point.

Decorative output must never reach a machine-readable channel: agents parse
`wiki` stdout as JSON, and the retrieval eval harness parses
`wiki_retrieval.index --status` stderr as JSON. So styling is gated on stderr
being an interactive terminal — under a pipe nothing extra is emitted at all.
"""

from __future__ import annotations

import os
import sys
import threading
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


class Spinner:
    """A single-line progress display that disappears cleanly in non-TTY use."""

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, text: str, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.text = text
        self.active = enabled(self.stream)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> "Spinner":
        if self.active:
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()
        return self

    def _animate(self) -> None:
        index = 0
        while not self._stop.wait(0.09):
            with self._lock:
                message = self.text
            rendered = paint(self.FRAMES[index % len(self.FRAMES)], CYAN, self.stream)
            self.stream.write(f"\r\x1b[2K  {rendered}  {message}")
            self.stream.flush()
            index += 1

    def update(self, text: str) -> None:
        with self._lock:
            self.text = text

    def finish(self, text: str, state: str = "ok") -> None:
        if not self.active:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        symbol = "✓" if state in {"ok", "committed"} else "!" if state == "warn" else "✗"
        color = status_color(state)
        self.stream.write(f"\r\x1b[2K  {paint(symbol, color, self.stream)}  {text}\n")
        self.stream.flush()

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._stop.is_set():
            self.finish("Interrupted" if exc_type else self.text, "fail" if exc_type else "ok")
