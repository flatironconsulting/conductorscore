"""E2E: a fresh Codex user installs ConductorScore via skills.sh and can run it.

This pins the canonical Codex install path end-to-end:

  1. start from a Codex sandbox with no conductorscore skill,
  2. confirm headless Codex cannot run it,
  3. install it with ``npx skills add`` (skills.sh) from this checkout,
  4. confirm headless Codex can now actually run it.

If step 4 fails, skills.sh installs the skill somewhere Codex can find it
(``~/.agents/skills``) but the skill can't be *run* there — typically because
SKILL.md hardcodes an install path (``~/.codex/skills/...``) that the skills.sh
layout doesn't match. The fix lives in the skill, not here.

The test installs from the local checkout (not the published GitHub slug) so it
validates the SKILL.md in *this* tree. It auto-skips when there's no usable
codex binary, no Codex auth to seed, or no npx — so the default offline ``pytest``
run stays green. It makes ~2 real ``codex exec`` model calls when it does run.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.e2e.codex_runner import (
    codex_auth_available,
    find_codex_binary,
    make_sandbox,
    model_reached,
    run_codex,
    skill_executed,
)

pytestmark = [pytest.mark.e2e, pytest.mark.codex]

# repo root: <root>/tests/e2e/this_file.py → parents[2] holds skills/conductorscore
SKILL_SOURCE = Path(__file__).resolve().parents[2]

RUN_PROMPT = (
    "You may have a skill named 'conductorscore'. If it is available to you, run "
    "it now exactly as its SKILL.md instructs: actually execute its run.py "
    "orchestrator as a shell command — do not merely describe it. If no such "
    "skill is available, reply with exactly NO_SKILL and run nothing."
)


@pytest.fixture(scope="module")
def codex_bin() -> Path:
    binary = find_codex_binary()
    if binary is None:
        pytest.skip("no runnable codex binary found")
    if not codex_auth_available():
        pytest.skip("no ~/.codex/auth.json to seed headless Codex")
    if shutil.which("npx") is None:
        pytest.skip("npx (skills.sh CLI) not available")
    return binary


def _installed_skill_files(env: dict) -> list[Path]:
    home = Path(env["HOME"])
    return [
        *home.glob(".agents/skills/conductorscore/SKILL.md"),
        *home.glob(".codex/skills/conductorscore/SKILL.md"),
    ]


def _install_via_skills_sh(env: dict) -> None:
    subprocess.run(
        [
            # -g installs at user scope ($HOME/.agents/skills) — the real "install
            # the skill for my Codex" flow. Without it the CLI defaults to project
            # scope (<cwd>/.agents/skills), which is not what a user runs.
            "npx", "-y", "skills", "add", str(SKILL_SOURCE),
            "--copy", "-g", "--skill", "conductorscore", "--agent", "codex", "-y",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=240,
        check=True,
    )


def test_skills_sh_install_makes_skill_runnable_in_codex(tmp_path: Path, codex_bin: Path):
    env = make_sandbox(tmp_path)
    home = Path(env["HOME"])

    # Step 1 — the sandbox starts with no conductorscore skill, nothing has run.
    assert not _installed_skill_files(env), "sandbox unexpectedly already has the skill"
    assert not skill_executed(home), "sandbox unexpectedly already has run.py artifacts"

    # Step 2 — headless Codex confirms it can't run a skill that isn't installed.
    before = run_codex(env, codex_bin, RUN_PROMPT)
    out_before = before.stdout + before.stderr
    if not model_reached(out_before):
        pytest.skip("headless Codex did not reach a model turn:\n" + out_before[-1000:])
    assert not skill_executed(home), (
        "run.py executed BEFORE the skill was installed — sandbox not clean:\n"
        + out_before[-1000:]
    )

    # Step 3 — install via skills.sh (npx) from this checkout. Installing copies
    # files only; it must not have run anything yet.
    _install_via_skills_sh(env)
    assert _installed_skill_files(env), "npx skills add did not install conductorscore"
    assert not skill_executed(home), "install unexpectedly executed run.py"

    # Step 4 — headless Codex can now actually RUN the installed skill. Success is
    # measured by run.py's startup side-effects (device id / cache dir), which a
    # model cannot fabricate by reading SKILL.md.
    after = run_codex(env, codex_bin, RUN_PROMPT)
    out_after = after.stdout + after.stderr
    if not model_reached(out_after):
        pytest.skip("headless Codex did not reach a model turn on 2nd call:\n" + out_after[-1000:])
    assert skill_executed(home), (
        "Installed via skills.sh (npx) into the Codex skill layout, but Codex "
        "could NOT run the skill (run.py never executed). SKILL.md likely "
        "hardcodes an install path that the skills.sh layout (~/.agents/skills) "
        "does not match — make the run instruction resolve relative to the "
        "skill's own directory.\n" + out_after[-2000:]
    )
