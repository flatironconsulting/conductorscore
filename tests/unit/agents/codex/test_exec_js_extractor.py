"""Codex 0.14x ``exec``-cell shell extraction (precision-first).

Modern Codex runs shell inside a JS runtime cell: ``custom_tool_call`` name
``exec`` whose ``input`` is raw JavaScript, with shell commands as literal
arguments of ``shell(...)`` calls (observed live: ``await shell(`git ...`)``).
``extract_shell_commands_from_exec_js`` extracts ONLY literal arguments —
template literals without ``${`` interpolation, plain single/double-quoted
strings. Dynamically-composed commands are deliberately missed: an undercount
beats a false commit/revert count.
"""
from __future__ import annotations

from scripts.agents.codex.taxonomy import extract_shell_commands_from_exec_js


def test_template_literal_extracted():
    src = "const out = await shell(`git commit -m \"feat: x\"`); text(out);"
    assert extract_shell_commands_from_exec_js(src) == (
        'git commit -m "feat: x"',
    )


def test_double_and_single_quoted_extracted():
    src = "await shell(\"git status\"); await shell('ls -la');"
    assert extract_shell_commands_from_exec_js(src) == ("git status", "ls -la")


def test_interpolated_template_skipped():
    """``${...}`` means the command is dynamically composed — skip it
    (precision over recall)."""
    src = "const f = 'x.py'; await shell(`git add ${f} && git commit -m y`);"
    assert extract_shell_commands_from_exec_js(src) == ()


def test_multiple_calls_in_order():
    src = (
        "const a = await shell(`git stash drop`);\n"
        "if (a.ok) { await shell(`git clean -f`); }"
    )
    assert extract_shell_commands_from_exec_js(src) == (
        "git stash drop",
        "git clean -f",
    )


def test_escaped_backtick_inside_template():
    src = "await shell(`echo \\`hello\\``);"
    assert extract_shell_commands_from_exec_js(src) == ("echo `hello`",)


def test_no_shell_calls_returns_empty():
    src = "const x = ALL_TOOLS.filter(t => /git commit/.test(t.name));"
    assert extract_shell_commands_from_exec_js(src) == ()


def test_non_string_and_empty_inputs_safe():
    assert extract_shell_commands_from_exec_js("") == ()
    assert extract_shell_commands_from_exec_js(None) == ()  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The REAL invocation shape in live 0.144.5 rollouts (golden-data recon,
# 696 call sites): ``tools.exec_command({cmd: "<command>", ...})`` — the old
# exec_command tool surfaced as a JS function with the same ``{cmd:...}``
# arg shape. ``shell(...)`` support above is kept for other builds.
# ---------------------------------------------------------------------------


def test_tools_exec_command_cmd_literal_extracted():
    src = 'const r = await tools.exec_command({cmd: "git status", workdir: "/p"});'
    assert extract_shell_commands_from_exec_js(src) == ("git status",)


def test_tools_exec_command_quoted_key_and_backtick_value():
    src = "await tools.exec_command({\"cmd\":`git commit -m 'synthetic'`});"
    assert extract_shell_commands_from_exec_js(src) == (
        "git commit -m 'synthetic'",
    )


def test_tools_exec_command_interpolated_cmd_skipped():
    src = "await tools.exec_command({cmd: `git add ${f}`});"
    assert extract_shell_commands_from_exec_js(src) == ()


def test_mixed_shell_and_exec_command_in_source_order():
    src = (
        "await tools.exec_command({cmd: 'git fetch'});\n"
        "await shell(`git status`);\n"
        "await tools.exec_command({cmd: \"git commit -m x\"});"
    )
    assert extract_shell_commands_from_exec_js(src) == (
        "git fetch",
        "git status",
        "git commit -m x",
    )


def test_cmd_key_outside_exec_command_call_not_extracted():
    """A bare ``cmd:`` key in unrelated data must not be read as a shell
    command — only keys reached from a ``tools.exec_command(`` call site."""
    src = 'const cfg = {cmd: "git push --force"}; console.log(cfg);'
    assert extract_shell_commands_from_exec_js(src) == ()
