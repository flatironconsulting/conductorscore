"""Validate SKILL.md frontmatter and body for the conductorscore skill (web-first flow).

SKILL.md is the entry point that the Claude Code skills system reads to know
how to invoke our extractor. The frontmatter is YAML; we parse it manually
(stdlib-only) so the test stays portable.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# SKILL.md is the self-contained skill package's entry point. It was relocated
# from the repo root to skills/conductorscore/ (gh skill discovers skills at
# skills/*/SKILL.md; the root copy was retired with the pairing-code flow).
SKILL_MD = REPO_ROOT / "skills" / "conductorscore" / "SKILL.md"


def _read_skill_md() -> str:
    assert SKILL_MD.exists(), f"SKILL.md missing at {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


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


def test_body_points_at_run_py():
    _, body = _parse_frontmatter(_read_skill_md())
    assert "scripts/run.py" in body


def test_body_describes_device_flow_auth():
    _, body = _parse_frontmatter(_read_skill_md())
    # Device-flow first run: the skill surfaces a github.com/login/device URL the
    # agent relays to the user (pairing codes / pair.py are deprecated).
    assert "login/device" in body, (
        "SKILL.md must describe the GitHub device-flow login (login/device URL)"
    )
    assert "pair.py" not in body, (
        "SKILL.md must NOT reference pair.py (pairing-code flow is deprecated)"
    )


def test_body_describes_ask_relay_protocol():
    _, body = _parse_frontmatter(_read_skill_md())
    # The agent must know to relay CONDUCTORSCORE_ASK lines as multiple-choice
    # and re-run run.py with the matching flag.
    assert "CONDUCTORSCORE_ASK" in body, (
        "SKILL.md must document the CONDUCTORSCORE_ASK relay protocol"
    )
    assert "--providers" in body and "--daily" in body, (
        "SKILL.md must mention the --providers / --daily re-run flags"
    )


def test_body_has_no_old_auth_subcommands():
    _, body = _parse_frontmatter(_read_skill_md())
    # Old inline-auth subcommands must be gone in the device-flow flow.
    for old_sub in ("auth github", "auth email start", "auth anonymous"):
        assert old_sub not in body, (
            f"SKILL.md must not reference old subcommand {old_sub!r} "
            f"(removed in device-flow flow)"
        )


def test_body_has_no_hardcoded_oauth_client_id():
    _, body = _parse_frontmatter(_read_skill_md())
    assert "Ov23litmD8cHtbJmD12h" not in body, (
        "SKILL.md must not contain the hardcoded GitHub OAuth client_id"
    )
