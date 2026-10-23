"""Claude Code session discovery.

Honors ``CONDUCTORSCORE_CLAUDE_HOME`` (the ``.claude`` directory) and globs
``<home>/projects/<dir>/*.jsonl``. This is the real implementation; the
legacy ``scripts.events`` facade re-exports ``claude_home`` / ``find_sessions``
from here so existing imports keep working unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.agents.base import (
    first_ts_from_head,
    last_ts_from_tail,
    modified_before,
    preflight_from,
)
from scripts.core.normalized import SessionMeta
from scripts.core.timestamps import parse_iso_ts_ms


def _parse_ts_ms(line) -> int | None:
    """Parse a Claude Code timestamp from a JSONL line (string) or dict.

    Returns epoch milliseconds, or None on any parse failure.
    """
    if isinstance(line, str):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
    elif isinstance(line, dict):
        d = line
    else:
        return None
    if not isinstance(d, dict):
        return None
    return parse_iso_ts_ms(d.get("timestamp"))


def _project_root_from_dir(dir_name: str) -> str:
    """Reconstruct project root path from Claude Code dir naming.

    Claude Code stores transcripts under `~/.claude/projects/<dir>` where
    `<dir>` is the absolute project root with `/` replaced by `-`. So
    `-home-dev-conductorscore-client` -> `/home/dev/conductorscore/client`.

    Note: this mapping is lossy (original `-` chars become `/`) but that is
    acceptable because the value is hashed before transmission.
    """
    return "/" + dir_name.lstrip("-").replace("-", "/")


def subagent_transcripts(jsonl_path: Path) -> list[Path]:
    """Every subagent transcript belonging to a Claude session, recursively.

    Claude Code writes a session's subagent (sidechain) transcripts next to
    its main JSONL, and Workflow agents one level deeper::

        <project>/<session_id>.jsonl
        <project>/<session_id>/subagents/agent-<id>.jsonl
        <project>/<session_id>/subagents/workflows/wf_<run>/agent-<id>.jsonl

    Returns them sorted (deterministic), or ``[]`` when the session has no
    ``subagents/`` directory. The single place that knows this layout: the
    token reader, the turn aggregate, the subagent panels and the feature
    cache fingerprint all go through it.
    """
    jsonl_path = Path(jsonl_path)
    root = jsonl_path.parent / jsonl_path.stem / "subagents"
    if not root.is_dir():
        return []
    try:
        return sorted(p for p in root.rglob("agent-*.jsonl") if p.is_file())
    except OSError:
        return []


def claude_home() -> Path:
    return Path(
        os.environ.get("CONDUCTORSCORE_CLAUDE_HOME", str(Path.home() / ".claude"))
    )


def preflight(now_ms: int, window_ms: int) -> dict:
    """Metadata-only probe of the Claude home for cross-provider consent.

    Returns ONLY counts — never parses transcript message text, tool
    inputs/outputs, cwd, or instruction files. The session-window count comes
    from each transcript's first/last timestamp rows (the same cheap fields
    ``find_sessions`` reads to bound a session), NOT from any message body.
    Used to decide whether to ASK the user for permission to scan the
    non-launched provider; the real scan only runs after consent.

    Keys:
      * ``home_exists``    — the ``.claude`` dir is present.
      * ``config_exists``  — a ``settings.json`` is present (Claude config).
      * ``sessions_in_window`` — count of transcripts with activity within
        ``now_ms - window_ms``.
      * ``sessions_per_day`` — approximate sessions/day across the window.
    """
    home = claude_home()
    cutoff = now_ms - window_ms

    def _last_ts_iter():
        projects_dir = home / "projects"
        if not projects_dir.is_dir():
            return
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl in proj_dir.glob("*.jsonl"):
                # Only the last timestamp matters: skip files untouched since
                # the cutoff, read just the tail of the rest.
                if modified_before(jsonl, cutoff):
                    continue
                yield last_ts_from_tail(jsonl, _parse_ts_ms)

    return preflight_from(home, "settings.json", _last_ts_iter(), now_ms, window_ms)


def find_sessions() -> list[SessionMeta]:
    home = claude_home()
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []
    out: list[SessionMeta] = []
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        project_root = _project_root_from_dir(proj_dir.name)
        for jsonl in proj_dir.glob("*.jsonl"):
            session_id = jsonl.stem
            # Runs before every scan over every transcript ever written —
            # read only the ends, never whole files (GBs for a heavy user).
            first = first_ts_from_head(jsonl, _parse_ts_ms)
            last = last_ts_from_tail(jsonl, _parse_ts_ms)
            if first is None or last is None:
                continue
            out.append(
                SessionMeta(
                    session_id=session_id,
                    project_root=project_root,
                    first_ts_ms=first,
                    last_ts_ms=last,
                    jsonl_path=jsonl,
                )
            )
    return out


__all__ = ["claude_home", "find_sessions", "subagent_transcripts"]
