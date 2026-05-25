"""Tests for scripts/env_loader.py — auto-load .env.local at startup."""
import os
from pathlib import Path

from scripts.env_loader import load_env_local


def test_loads_keys_from_env_local(tmp_path, monkeypatch):
    monkeypatch.delenv("FOO_TEST_VAR", raising=False)
    monkeypatch.delenv("QUOTED_VAR", raising=False)
    (tmp_path / ".env.local").write_text(
        "FOO_TEST_VAR=bar\n"
        "QUOTED_VAR=\"hello world\"\n"
        "# comment\n"
        "no_equals_line\n"
        "\n"
    )
    load_env_local(skill_dir=tmp_path)
    assert os.environ["FOO_TEST_VAR"] == "bar"
    assert os.environ["QUOTED_VAR"] == "hello world"


def test_does_not_overwrite_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALREADY_SET", "real-value")
    (tmp_path / ".env.local").write_text("ALREADY_SET=should-not-overwrite\n")
    load_env_local(skill_dir=tmp_path)
    assert os.environ["ALREADY_SET"] == "real-value"


def test_no_env_file_is_silent(tmp_path):
    # No .env.local in tmp_path — should not raise.
    load_env_local(skill_dir=tmp_path)
