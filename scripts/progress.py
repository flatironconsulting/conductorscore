"""Streaming progress bar.

Two modes, picked from ``stdout.isatty()`` (override via ``force_tty=``):

* **TTY** (real terminal) — rewrites a single line via ``\\r`` so the bar
  animates in place. Throttled by ``min_pct_delta`` / ``min_interval_s``.
* **Non-TTY** (captured: Claude Code Bash tool, CI, pipes) — emits only at
  50% and 100% so transcripts don't accumulate dozens of bar lines.
"""
from __future__ import annotations

import sys
import time
from typing import IO


class ProgressBar:
    def __init__(
        self,
        total: int,
        *,
        width: int = 20,
        out: IO[str] | None = None,
        min_pct_delta: float = 0.05,
        min_interval_s: float = 1.0,
        prefix: str = "   ",
        force_tty: bool | None = None,
    ):
        self.total = max(0, total)
        self.width = width
        self.out = out or sys.stdout
        self.min_pct_delta = min_pct_delta
        self.min_interval_s = min_interval_s
        self.prefix = prefix
        self.is_tty = (
            force_tty if force_tty is not None else getattr(self.out, "isatty", lambda: False)()
        )
        self._last_pct = -1.0
        self._last_t = 0.0
        self._done = 0
        self._emitted_milestones: set[float] = set()
        self._tty_line_open = False

    def update(self, done: int) -> None:
        self._done = done
        if self.total == 0:
            return
        pct = done / self.total
        if pct >= 1.0:
            return  # final emission belongs to done()
        if self.is_tty:
            now = time.monotonic()
            if (pct - self._last_pct) >= self.min_pct_delta or (
                now - self._last_t
            ) >= self.min_interval_s:
                self._emit(pct)
                self._last_pct = pct
                self._last_t = now
        else:
            for ms in (0.5,):
                if pct >= ms and ms not in self._emitted_milestones:
                    self._emit(ms)
                    self._emitted_milestones.add(ms)

    def done(self) -> None:
        if self.total == 0:
            return
        self._done = self.total
        self._emit(1.0)
        if self.is_tty and self._tty_line_open:
            self.out.write("\n")
            self.out.flush()
            self._tty_line_open = False

    def _emit(self, pct: float) -> None:
        filled = int(self.width * pct)
        bar = "█" * filled + "░" * (self.width - filled)
        line = f"{self.prefix}[{bar}]  {self._done}/{self.total} {int(pct * 100):>3}%"
        if self.is_tty:
            self.out.write(f"\r{line}")
            self._tty_line_open = True
        else:
            self.out.write(f"{line}\n")
        self.out.flush()
