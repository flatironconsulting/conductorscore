"""scripts.version_check: the update notice, and the pending notice a headless
daily scan records for the next session start."""
from __future__ import annotations

import subprocess

import scripts.version_check as vc


def test_is_outdated_compares_numerically():
    assert vc.is_outdated("0.7.9", "0.7.10")
    assert not vc.is_outdated("0.7.10", "0.7.9")
    assert not vc.is_outdated("0.7.6", "0.7.6")


def test_notice_names_a_command_that_applies_without_a_prompt():
    line = vc.notice("0.7.7")
    assert line == "↑ v0.7.7 available — run: gh skill update conductorscore --all --unpin"


def test_update_cmd_flags_exist_in_gh_skill_update():
    # The command must stay valid against the real CLI when it's installed.
    try:
        out = subprocess.run(["gh", "skill", "update", "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return
    if out.returncode != 0:
        return
    for flag in ("--all", "--unpin"):
        assert flag in out.stdout + out.stderr


def test_pending_round_trip(tmp_path):
    vc.record_pending(tmp_path, "0.7.6", "0.7.7")
    assert vc.pending(tmp_path, "0.7.6") == "0.7.7"
    # Once the user has updated, the recorded notice no longer applies.
    assert vc.pending(tmp_path, "0.7.7") is None


def test_record_pending_clears_when_current_or_unknown(tmp_path):
    vc.record_pending(tmp_path, "0.7.6", "0.7.7")
    vc.record_pending(tmp_path, "0.7.7", "0.7.7")
    assert vc.pending(tmp_path, "0.7.6") is None
    vc.record_pending(tmp_path, "0.7.6", "0.7.7")
    vc.record_pending(tmp_path, "0.7.6", None)  # server unreachable: forget it
    assert vc.pending(tmp_path, "0.7.6") is None


def test_pending_ignores_a_corrupt_file(tmp_path):
    (tmp_path / "update-available.json").write_text("not json")
    assert vc.pending(tmp_path, "0.7.6") is None
