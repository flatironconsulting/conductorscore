"""Streaming progress bar for the TUI (one line per emission)."""
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
    ):
        self.total = max(0, total)
        self.width = width
        self.out = out or sys.stdout
        self.min_pct_delta = min_pct_delta
        self.min_interval_s = min_interval_s
        self.prefix = prefix
        self._last_pct = -1.0
        self._last_t = 0.0
        self._done = 0

    def update(self, done: int) -> None:
        self._done = done
        if self.total == 0:
            return
        pct = done / self.total
        now = time.monotonic()
        if pct >= 1.0:
            return  # final line emitted by done()
        if (pct - self._last_pct) >= self.min_pct_delta or (
            now - self._last_t
        ) >= self.min_interval_s:
            self._emit(pct)
            self._last_pct = pct
            self._last_t = now

    def done(self) -> None:
        if self.total == 0:
            return
        self._done = self.total
        self._emit(1.0)

    def _emit(self, pct: float) -> None:
        filled = int(self.width * pct)
        bar = "█" * filled + "░" * (self.width - filled)
        self.out.write(
            f"{self.prefix}[{bar}]  {self._done}/{self.total} {int(pct * 100):>3}%\n"
        )
        self.out.flush()
