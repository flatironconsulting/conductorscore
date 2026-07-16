"""Helpers for the skills.sh → Codex install E2E test.

Locates a working ``codex`` binary, builds an isolated sandbox
(``HOME`` + ``CODEX_HOME``) seeded with the user's real Codex auth, and runs
``codex exec`` headless. Nothing here touches the real ``~/.codex`` or
``~/.agents`` beyond *reading* the auth files to copy them into the sandbox.

Why a separate CODEX_HOME: Codex reads auth/config from ``$CODEX_HOME`` (default
``~/.codex``) but discovers user-scope skills under ``$HOME/.agents/skills`` —
so isolating both lets a test install a skill and run Codex against it without
disturbing the developer's real setup.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Detecting that run.py actually EXECUTED cannot rely on its stdout: the skill's
# whole observable output (``Version X.Y.Z``, ``CONDUCTORSCORE_ASK``, …) is
# documented verbatim in SKILL.md, so a model that merely *reads* SKILL.md can
# reproduce any text marker without running anything. Instead we key on
# filesystem side-effects that only appear when run.py runs: it writes a device
# identity and a cache dir at startup.
RUNTIME_ARTIFACTS = (
    Path(".config/conductorscore/device_id"),
    Path(".cache/conductorscore"),
)

# Codex prints this once a model turn completes; absence means we never reached
# the model (no auth / no network) → the caller should skip rather than fail.
MODEL_REACHED_MARKER = "tokens used"


def find_codex_binary() -> Path | None:
    """Return a runnable ``codex`` path, or ``None`` if none is usable.

    Prefers ``codex`` on PATH, but only if its resolved target exists — a common
    breakage is ``~/.local/bin/codex`` symlinking into a VS Code extension dir
    that got upgraded out from under it. Falls back to the binary bundled in the
    newest installed ``openai.chatgpt-*`` extension.
    """
    which = shutil.which("codex")
    if which and Path(which).resolve().exists():
        return Path(which)

    ext = Path.home() / ".vscode-server" / "extensions"
    candidates = sorted(
        ext.glob("openai.chatgpt-*/bin/*/codex"),
        key=lambda p: p.parts[p.parts.index("extensions") + 1],  # extension version dir
        reverse=True,
    )
    for cand in candidates:
        if cand.exists() and os.access(cand, os.X_OK):
            return cand
    return None


def codex_auth_available() -> bool:
    """True if there's a real ``~/.codex/auth.json`` to seed the sandbox with."""
    return (Path.home() / ".codex" / "auth.json").is_file()


def _strip_mcp(toml_text: str) -> str:
    """Drop ``[mcp_servers.*]`` tables from a config.toml.

    Headless Codex can't complete interactive MCP OAuth, so seeded MCP servers
    just emit transport errors and add latency. Everything else (model, provider,
    auth preferences) is preserved.
    """
    out, skipping = [], False
    for line in toml_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped.startswith("[mcp_servers")
        if not skipping:
            out.append(line)
    return "\n".join(out) + "\n"


def make_sandbox(tmp: Path) -> dict:
    """Build an isolated env dict for headless Codex rooted under ``tmp``.

    Creates ``tmp/home`` (HOME), ``tmp/home/.codex`` (CODEX_HOME) seeded with the
    real ``auth.json`` and an MCP-stripped ``config.toml``, and ``tmp/home/work``
    as the working directory.
    """
    home = tmp / "home"
    codex_home = home / ".codex"
    work = home / "work"
    codex_home.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    real = Path.home() / ".codex"
    shutil.copyfile(real / "auth.json", codex_home / "auth.json")
    config = real / "config.toml"
    if config.is_file():
        (codex_home / "config.toml").write_text(_strip_mcp(config.read_text()))

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(codex_home)
    env["WORK_DIR"] = str(work)
    return env


def run_codex(env: dict, codex_bin: Path, prompt: str, timeout: int = 300):
    """Run ``codex exec`` headless and return the CompletedProcess.

    Uses ``--dangerously-bypass-approvals-and-sandbox`` so the model may actually
    execute the skill's shell command (the run is already externally isolated via
    the sandbox env), and ``-c mcp_servers={}`` as belt-and-suspenders alongside
    the stripped config.
    """
    return subprocess.run(
        [
            str(codex_bin),
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            "mcp_servers={}",
            prompt,
        ],
        cwd=env["WORK_DIR"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def model_reached(output: str) -> bool:
    """True if Codex actually completed a model turn (vs. an auth/network skip)."""
    return MODEL_REACHED_MARKER in output


def skill_executed(home: Path) -> bool:
    """True if conductorscore's ``run.py`` actually ran under this sandbox HOME.

    Checks for the filesystem artifacts run.py writes at startup (device id +
    cache dir). These exist only when the script executes — not when SKILL.md is
    merely read — so they distinguish a real run from a model paraphrasing the
    skill's documented output.
    """
    return any((home / rel).exists() for rel in RUNTIME_ARTIFACTS)
