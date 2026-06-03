"""Atomic JSON status writer for the scan subprocess.

The orchestrator polls the same file; atomic rename ensures the poller never
reads a partial write.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class StatusWriter:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **fields) -> None:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=str(self.path.parent),
            prefix=".status.",
            suffix=".tmp",
        )
        try:
            json.dump(fields, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self.path)
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
