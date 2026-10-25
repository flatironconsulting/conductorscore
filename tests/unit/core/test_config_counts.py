from scripts.agents.claude.config import _count_skill_dirs as claude_skills
from scripts.agents.codex.config import _count_skills as codex_skills
from scripts.agents.cursor.config import _count_skill_dirs as cursor_skills
from scripts.core.config_counts import count_lines, count_skill_dirs


def test_symlinked_skill_counted_once_claude(tmp_path):
    real = tmp_path / ".agents" / "skills" / "x"
    real.mkdir(parents=True)
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "x").symlink_to(real, target_is_directory=True)
    assert claude_skills(tmp_path, None) == 1


def test_symlinked_skill_counted_once_codex(tmp_path):
    real = tmp_path / "superpowers" / "skills" / "x"
    real.mkdir(parents=True)
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "x").symlink_to(real, target_is_directory=True)
    assert codex_skills(tmp_path) == 1


def test_symlinked_skill_counted_once_cursor(tmp_path):
    real = tmp_path / "skills" / "x"
    real.mkdir(parents=True)
    (tmp_path / "skills" / "y").symlink_to(real, target_is_directory=True)
    assert cursor_skills(tmp_path) == 1


def test_count_skill_dirs_skips_hidden_files_missing(tmp_path):
    (tmp_path / "s" / "a").mkdir(parents=True)
    (tmp_path / "s" / ".hid").mkdir()
    (tmp_path / "s" / "f.md").write_text("x")
    assert count_skill_dirs([tmp_path / "s", tmp_path / "nope", tmp_path / "s"]) == 1


def test_count_lines(tmp_path):
    assert count_lines(tmp_path / "missing") == 0
    assert count_lines(tmp_path) == 0  # directory
    f = tmp_path / "f"
    f.write_text("")
    assert count_lines(f) == 0
    f.write_text("a\nb\n")
    assert count_lines(f) == 2
    f.write_text("a\nb")
    assert count_lines(f) == 2
    f.write_bytes(b"a\n\xff\nb\n")
    assert count_lines(f) == 3
