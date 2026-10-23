"""Is a newer ConductorScore skill published, and how does the user get it?

Shared by run.py (the update line in the banner), scan.py (a headless daily
scan records a pending notice, since no one sees its output) and daily.py
(the next session start shows that recorded notice). Never raises.
"""
from __future__ import annotations

import json
from pathlib import Path

# `--all` applies without a confirmation prompt (an agent's shell has no TTY,
# so a bare `gh skill update` only lists the update), the skill name limits it
# to ConductorScore, and `--unpin` includes installs made with `--pin`, which
# `gh skill update` otherwise skips.
UPDATE_CMD = "gh skill update conductorscore --all --unpin"
_PENDING_FILE = "update-available.json"


def skill_version() -> str | None:
    """The installed skill's version, from the VERSION file beside scripts/."""
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def is_outdated(local: str, latest: str) -> bool:
    def parts(v: str) -> list[int]:
        out = []
        for p in v.strip().split("."):
            try:
                out.append(int(p))
            except ValueError:
                out.append(0)
        return out

    return parts(local) < parts(latest)


def latest_version(api_base: str) -> str | None:
    """The server's published skill version, or None on any failure."""
    try:
        from scripts._http import get_json

        status, body = get_json(f"{api_base}/api/skill-config")
        latest = body.get("latest_skill_version") if status == 200 else None
    except Exception:
        return None
    return latest if isinstance(latest, str) and latest else None


def notice(latest: str) -> str:
    return f"↑ v{latest} available — run: {UPDATE_CMD}"


def record_pending(cache_dir: Path, local: str | None, latest: str | None) -> None:
    """Remember a newer version for the next session start, or forget a stale one."""
    path = cache_dir / _PENDING_FILE
    try:
        if local and latest and is_outdated(local, latest):
            path.write_text(json.dumps({"latest": latest}), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def pending(cache_dir: Path, local: str | None) -> str | None:
    """A recorded newer version that is still newer than ``local``, else None."""
    try:
        latest = json.loads((cache_dir / _PENDING_FILE).read_text(encoding="utf-8")).get("latest")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(latest, str) and local and is_outdated(local, latest):
        return latest
    return None
