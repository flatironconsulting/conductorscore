from scripts.agents.codex.taxonomy import skill_names_from_shell_command


def test_skill_names_from_shell_command_handles_agents_skills_path():
    cmd = "sed -n '1,80p' /home/u/.agents/skills/report-quality-review/SKILL.md"
    assert skill_names_from_shell_command(cmd) == ("report-quality-review",)


def test_skill_names_rejects_globs_and_dedupes():
    cmd = (
        "cat /a/.codex/skills/browser/SKILL.md "
        "/a/.codex/skills/*/SKILL.md "
        "/a/.codex/skills/browser/SKILL.md"
    )
    assert skill_names_from_shell_command(cmd) == ("browser",)
