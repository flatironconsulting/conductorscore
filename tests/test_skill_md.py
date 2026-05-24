"""Validate SKILL.md frontmatter and body for the conductorscore skill.

SKILL.md is the entry point that the Claude Code skills system reads to know
how to invoke our extractor. The frontmatter is YAML; we parse it manually
(stdlib-only) so the test stays portable.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "SKILL.md"


def _read_skill_md() -> str:
    assert SKILL_MD.exists(), f"SKILL.md missing at {SKILL_MD}"
    return SKILL_MD.read_text()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a markdown file with `---`-delimited YAML frontmatter.

    Returns (frontmatter_dict, body). We only need scalar key: value pairs;
    no nested structures. Values may be unquoted or wrapped in matching
    single/double quotes (we strip one pair if present).
    """
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", "missing opening --- fence"
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    assert end_idx is not None, "missing closing --- fence"
    fm: dict[str, str] = {}
    for raw in lines[1:end_idx]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        assert ":" in raw, f"bad frontmatter line: {raw!r}"
        key, _, val = raw.partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        fm[key.strip()] = val
    body = "\n".join(lines[end_idx + 1 :])
    return fm, body


def test_skill_md_exists():
    assert SKILL_MD.exists()


def test_frontmatter_name_is_conductorscore():
    fm, _ = _parse_frontmatter(_read_skill_md())
    assert fm.get("name") == "conductorscore"


def test_frontmatter_description_is_nonempty_and_mentions_command():
    fm, _ = _parse_frontmatter(_read_skill_md())
    desc = fm.get("description", "")
    assert desc, "description must be non-empty"
    assert "/conductorscore" in desc, (
        f"description must mention /conductorscore so the skill router "
        f"knows when to trigger; got: {desc!r}"
    )


def test_body_lists_all_four_subcommands():
    _, body = _parse_frontmatter(_read_skill_md())
    # The four user-visible invocations described in the run.py CLI.
    required = [
        "/conductorscore",
        "/conductorscore pair",
        "/conductorscore --explain",
        "/conductorscore --dry-run",
    ]
    for cmd in required:
        assert cmd in body, f"SKILL.md body must list {cmd!r}"


def test_body_points_at_run_py():
    _, body = _parse_frontmatter(_read_skill_md())
    # The skill must instruct callers to invoke scripts/run.py — that's the
    # one entrypoint we ship and version. Without this line the skill router
    # would have to guess.
    assert "scripts/run.py" in body
