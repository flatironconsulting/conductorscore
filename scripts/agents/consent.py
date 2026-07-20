"""Cross-provider consent for ConductorScore scans.

ConductorScore launches from one provider (Claude or Codex). When it detects
recent activity from the OTHER provider, it must NOT silently scan it — it
asks the user for permission first. This module owns:

  * launch-provider detection (``CONDUCTORSCORE_LAUNCH_PROVIDER``, falling back
    to install-path / command context),
  * the metadata-only preflight of the non-launched provider (delegated to the
    adapter's ``preflight`` — counts only, no transcript parsing),
  * the consent-cache read/write (``~/.cache/conductorscore/provider-consent.json``,
    scoped by API base AND account/session identity), and
  * the decision: scan both (consent / override / cache) vs. scan only the
    launch provider and emit ``CONDUCTORSCORE_PERMISSION_NEEDED``.

Privacy invariants:
  * ``CONDUCTORSCORE_PROVIDERS`` is an explicit override that BYPASSES prompting
    entirely (no consent needed when it is set).
  * Preflight is metadata-only; we never read the other provider's transcript
    content unless consent (or override / cache) allows scanning it.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from scripts.agents.base import AgentAdapter, AgentId

# 30-day scoring window, mirroring scanner.WINDOW_MS (kept local to avoid an
# import cycle: scanner imports the registry which imports this module).
WINDOW_MS = 30 * 24 * 60 * 60 * 1000

CONSENT_SCHEMA_VERSION = 1

# Canonical adapter order — the provider list is always emitted sorted/canonical
# so the cache key and providers_seen are stable.
_CANONICAL_ORDER: list[AgentId] = ["claude", "codex", "cursor"]

# Human-readable labels for provider ids, shared by run.py/scan.py so the
# cross-provider prompts (TTY picker, ASK line, permission-needed wording)
# never hardcode a provider list — they iterate _CANONICAL_ORDER /
# detected/permission_needed and look the label up here.
PROVIDER_LABELS: dict[AgentId, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
}


def _cache_dir(env: Mapping[str, str]) -> Path:
    # CONDUCTORSCORE_CACHE_DIR is the already-resolved writable dir run.py chose
    # (and exports into the scan subprocess env). Honor it first — verbatim, no
    # /conductorscore subdir — so consent persists in the SAME place run.py reads
    # and the orchestrator + subprocess never split-brain.
    explicit = env.get("CONDUCTORSCORE_CACHE_DIR")
    if explicit:
        return Path(explicit)
    xdg = env.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"


def consent_file(env: Mapping[str, str] = os.environ) -> Path:
    """Path to the consent cache. ``CONDUCTORSCORE_CONSENT_FILE`` overrides it
    (tests point this at a tmp path)."""
    override = env.get("CONDUCTORSCORE_CONSENT_FILE")
    if override:
        return Path(override)
    return _cache_dir(env) / "provider-consent.json"


def detect_launch_provider(env: Mapping[str, str] = os.environ) -> AgentId:
    """Which provider this run was launched from.

    Precedence:
      1. ``CONDUCTORSCORE_LAUNCH_PROVIDER`` (claude|codex|cursor) — set by the
         install snippet for the surface the run launches from.
      2. install-path / command context heuristic: a ``.codex`` (or
         ``.cursor``) segment in the skill install path
         (``CONDUCTORSCORE_SKILL_DIR`` / argv[0]) implies a Codex (or Cursor)
         launch. Path separators are normalized (backslash to forward slash)
         before matching, so a Windows install path
         (``C:\\Users\\u\\.cursor\\skills\\conductorscore``) is detected the
         same as its POSIX equivalent.
      3. default ``claude``.
    """
    explicit = (env.get("CONDUCTORSCORE_LAUNCH_PROVIDER") or "").strip().lower()
    if explicit in _CANONICAL_ORDER:
        return explicit  # type: ignore[return-value]
    # Fall back to install path / command context only when the env var is
    # absent. A skill installed under ~/.codex/... launches from Codex; under
    # ~/.cursor/... launches from Cursor. Normalize backslashes to forward
    # slashes first — Windows skill dirs (CONDUCTORSCORE_SKILL_DIR) use
    # backslashes and would otherwise never match, silently defaulting to
    # "claude" (live Windows failure: a Cursor-launched run scanned as
    # launch-provider claude with 0 sessions).
    skill_dir = (env.get("CONDUCTORSCORE_SKILL_DIR") or "").lower().replace("\\", "/")
    if "/.codex/" in skill_dir or skill_dir.endswith("/.codex"):
        return "codex"
    if "/.cursor/" in skill_dir or skill_dir.endswith("/.cursor"):
        return "cursor"
    return "claude"


def _other_providers(launch: AgentId) -> list[AgentId]:
    """Every canonical provider except ``launch``, in canonical order."""
    return [p for p in _CANONICAL_ORDER if p != launch]


def _canonical(providers: set[AgentId] | list[AgentId]) -> list[AgentId]:
    s = set(providers)
    return [p for p in _CANONICAL_ORDER if p in s]


@dataclass
class ConsentDecision:
    """Outcome of the cross-provider consent gate.

    ``providers`` is the canonical ordered list of providers the scanner should
    actually scan. ``permission_needed`` lists the OTHER providers' ids (a
    tuple, canonical order, possibly more than one — e.g. launching from
    Claude with both Codex AND Cursor active) that have recent activity but
    the user has not consented and no override / cache allows scanning them —
    the scanner emits the permission-needed line for each and scans only
    ``providers`` (never a provider merely because it appears here).
    ``permission_sessions_30d`` maps each such provider id to its 30-day
    session count.
    """

    launch_provider: AgentId
    providers: list[AgentId]
    permission_needed: tuple[AgentId, ...] = ()
    permission_sessions_30d: dict[str, int] = field(default_factory=dict)
    source: str = "default"  # default|override|cache|consent-prompt
    preflight: dict = field(default_factory=dict)

    @property
    def both(self) -> bool:
        return len(self.providers) > 1

    @property
    def needs_permission(self) -> bool:
        """True when at least one non-scanned provider is awaiting consent."""
        return bool(self.permission_needed)


def _account_identity(env: Mapping[str, str]) -> str:
    """Stable per-account/session identity for scoping the cache.

    Reads the stored auth entry for the configured API base (github_username /
    email / a hash of the device token). Falls back to ``anon`` so the cache is
    still usable (scoped by API base alone) when auth can't be read. NEVER
    raises — consent must not break a scan.
    """
    try:
        import scripts.auth_store as auth_store

        api_base = (
            env.get("CONDUCTORSCORE_API_BASE", "https://conductorscore.com")
            .rstrip("/")
        )
        entry = auth_store.load_auth(api_base)
    except Exception:
        entry = None
    if not isinstance(entry, dict):
        return "anon"
    ident = entry.get("github_username") or entry.get("email")
    if ident:
        return str(ident)
    tok = entry.get("device_token")
    if tok:
        import hashlib

        return "tok:" + hashlib.sha256(str(tok).encode()).hexdigest()[:16]
    return "anon"


def _cache_key(env: Mapping[str, str]) -> tuple[str, str]:
    api_base = (
        env.get("CONDUCTORSCORE_API_BASE", "https://conductorscore.com")
        .rstrip("/")
    )
    return api_base, _account_identity(env)


def read_cached_consent(env: Mapping[str, str] = os.environ) -> list[AgentId] | None:
    """Return the cached consented provider list for the current API base +
    account, or ``None`` when absent / disabled / stale-schema.

    ``CONDUCTORSCORE_DISABLE_CONSENT_CACHE`` bypasses the cache entirely.
    """
    if env.get("CONDUCTORSCORE_DISABLE_CONSENT_CACHE"):
        return None
    path = consent_file(env)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != CONSENT_SCHEMA_VERSION:
        return None
    api_base, ident = _cache_key(env)
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return None
    entry = entries.get(f"{api_base}\n{ident}")
    if not isinstance(entry, dict):
        return None
    providers = entry.get("providers")
    if not isinstance(providers, list):
        return None
    valid = [p for p in providers if p in _CANONICAL_ORDER]
    return _canonical(valid) if valid else None


def write_cached_consent(
    providers: list[AgentId], env: Mapping[str, str] = os.environ
) -> None:
    """Persist consent for the current API base + account. No-op when the cache
    is disabled. Never raises."""
    if env.get("CONDUCTORSCORE_DISABLE_CONSENT_CACHE"):
        return
    path = consent_file(env)
    api_base, ident = _cache_key(env)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != CONSENT_SCHEMA_VERSION:
            data = {"schema_version": CONSENT_SCHEMA_VERSION, "entries": {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"schema_version": CONSENT_SCHEMA_VERSION, "entries": {}}
    entries = data.setdefault("entries", {})
    entries[f"{api_base}\n{ident}"] = {
        "providers": _canonical(providers),
        "api_base": api_base,
        "timestamp_ms": int(time.time() * 1000),
        "schema_version": CONSENT_SCHEMA_VERSION,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def detect_agents(
    env: Mapping[str, str] = os.environ,
    *,
    now_ms: int | None = None,
    adapters: Mapping[AgentId, AgentAdapter] | None = None,
) -> list[AgentId]:
    """Metadata-only: which coding agents have sessions in the 30-day window.

    Preflights EVERY known agent (not just the non-launch one) so the
    orchestrator can ask the user UP FRONT which agents to scan when more than
    one is present. Reads only file existence / timestamp metadata — never
    transcript text, tool IO, cwd, or instruction files. Returns the agents in
    canonical order (claude, codex, cursor).
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if adapters is None:
        adapters = _default_adapters()
    found: list[AgentId] = []
    for aid in _CANONICAL_ORDER:
        ad = adapters.get(aid)
        if ad is None:
            continue
        pf = ad.preflight(now_ms, WINDOW_MS)
        if pf.get("home_exists") and int(pf.get("sessions_in_window", 0) or 0) > 0:
            found.append(aid)
    return found


def _default_adapters() -> dict[AgentId, AgentAdapter]:
    from scripts.agents.claude import ClaudeAdapter
    from scripts.agents.codex import CodexAdapter
    from scripts.agents.cursor import CursorAdapter

    return {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "cursor": CursorAdapter()}


def decide(
    env: Mapping[str, str] = os.environ,
    *,
    now_ms: int | None = None,
    adapters: Mapping[AgentId, AgentAdapter] | None = None,
) -> ConsentDecision:
    """Resolve which providers to scan, honoring override → cache → preflight.

    1. ``CONDUCTORSCORE_PROVIDERS`` set → explicit override, bypass prompting
       (and preflighting) entirely.
    2. else launch provider from ``CONDUCTORSCORE_LAUNCH_PROVIDER`` (or path).
    3. cached consent for this base+account → every OTHER provider it names is
       scanned straight through, no preflight, no prompt.
    4. every remaining OTHER provider (not launch, not already cached) is
       preflighted (metadata only). Any with activity in the 30-day window is
       added to ``permission_needed`` — it is NOT added to ``providers``. A
       provider is scanned (appears in ``providers``) ONLY because it is the
       launch provider, was override-selected, or was already
       cached/consented — NEVER merely because preflight found activity.
    5. if nothing needs asking, scan the launch (+ any cached) providers only.

    ``adapters`` lets tests inject fakes; defaults to live Claude/Codex/Cursor.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    launch = detect_launch_provider(env)

    # (1) Explicit override bypasses ALL prompting / preflight.
    from scripts.agents.registry import parse_agent_selection

    requested = env.get("CONDUCTORSCORE_PROVIDERS")
    if requested is not None and requested.strip() != "":
        return ConsentDecision(
            launch_provider=launch,
            providers=_canonical(parse_agent_selection(requested)),
            source="override",
        )

    others = _other_providers(launch)

    # (3) Cached consent — only the providers the cache actually names are
    # scanned straight through; anything else still needs a fresh preflight.
    cached = read_cached_consent(env)
    cached_set = set(cached) if cached is not None else set()
    consented_others = [p for p in others if p in cached_set]
    to_preflight = [p for p in others if p not in cached_set]

    if adapters is None:
        adapters = _default_adapters()

    # (4) Metadata-only preflight of every remaining (non-cached) provider.
    permission_needed: list[AgentId] = []
    permission_sessions_30d: dict[str, int] = {}
    last_preflight: dict = {}
    for p in to_preflight:
        adapter = adapters.get(p)
        if adapter is None:
            continue
        pf = adapter.preflight(now_ms, WINDOW_MS)
        last_preflight = pf
        sessions_30d = int(pf.get("sessions_in_window", 0) or 0)
        if pf.get("home_exists") and sessions_30d > 0:
            permission_needed.append(p)
            permission_sessions_30d[p] = sessions_30d

    providers = _canonical(set(consented_others) | {launch})

    if permission_needed:
        return ConsentDecision(
            launch_provider=launch,
            providers=providers,
            permission_needed=tuple(_canonical(permission_needed)),
            permission_sessions_30d=permission_sessions_30d,
            source="cache" if consented_others else "consent-prompt",
            preflight=last_preflight,
        )

    # (5) Nothing (further) to ask about.
    return ConsentDecision(
        launch_provider=launch,
        providers=providers,
        source="cache" if consented_others else "default",
        preflight=last_preflight,
    )


__all__ = [
    "CONSENT_SCHEMA_VERSION",
    "PROVIDER_LABELS",
    "WINDOW_MS",
    "ConsentDecision",
    "consent_file",
    "decide",
    "detect_agents",
    "detect_launch_provider",
    "read_cached_consent",
    "write_cached_consent",
]
