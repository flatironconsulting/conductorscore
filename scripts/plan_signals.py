"""Plan signal detector — strong + weak planning signals per outline.

Anchors:
- ``plans/003_outline.md`` § "Common definitions" (plan artifact, structured
  first prompt, strong/weak planning signal, planned).
- ``plans/004_wave1_implementation.md`` § Task 6.1.

Public surface:

- ``is_plan_shaped_path(path)`` — regex test for ``plans/``, ``specs/``,
  ``docs/{design,architecture,rfc,proposals}/``, OR basename containing
  ``plan``/``spec``/``design``/``rfc``. Rejects ``node_modules/``,
  ``vendor/``, ``.git/``, ``dist/``, ``build/``, ``target/`` and standard
  repo files (``README.md``, ``CHANGELOG.md``, ``LICENSE.md``,
  ``CONTRIBUTING.md``, ``CODE_OF_CONDUCT.md``, ``SECURITY.md``,
  ``CHANGES.md``).
- ``is_structured_prompt(text, token_count)`` — outline definition: >200
  tokens AND (≥2 markdown list lines OR ≥2 distinct sequence words from
  ``{step, phase, first, then, next, finally}``).
- ``detect_plan_signals(events, *, project_had_plan_artifact_prior_24h)``
  → ``PlanSignals(strong, weak, is_planned)``. Reads structural flags
  precomputed on each ``Event`` by the event reader. Never inspects raw
  prompt or tool input text.

Privacy: every signal name returned is a fixed categorical token — never
a file path, prompt fragment, or other raw transcript data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scripts.events import Event, EventKind, basename

# ---------------------------------------------------------------------------
# Path classification.
# ---------------------------------------------------------------------------

# Excluded path prefixes (per outline + plan).
_EXCLUDED_PATH_RE = re.compile(
    r"(^|/)(node_modules|vendor|\.git|dist|build|target)(/|$)",
)

# Plan-shaped directory regex.
_PLAN_DIR_RE = re.compile(
    r"(^|/)(plans?|specs?|docs/(design|architecture|rfc|proposals?))(/|$)",
    re.IGNORECASE,
)

# Plan-shaped basename: contains plan/spec/design/rfc as a substring.
_PLAN_BASENAME_RE = re.compile(r"(plan|spec|design|rfc)", re.IGNORECASE)

# Standard repo-root files that must not match even when basename heuristics
# would otherwise flag them. Lowercased compare.
_STANDARD_REPO_FILES: frozenset[str] = frozenset(
    {
        "readme.md",
        "changelog.md",
        "changes.md",
        "license.md",
        "contributing.md",
        "code_of_conduct.md",
        "security.md",
        "authors.md",
    }
)


def is_plan_shaped_path(path: str) -> bool:
    """Return True iff ``path`` matches the outline's plan-shaped pattern.

    Hits if:
      * The path includes a plan-shaped directory segment
        (``plans/``, ``specs/``, ``docs/design/`` …); OR
      * The basename (extension allowed; must be ``.md`` or ``.txt``)
        contains ``plan``, ``spec``, ``design``, or ``rfc`` and is not
        one of the standard repo-root files.

    Misses if the path lies under an excluded prefix
    (``node_modules/``, ``vendor/``, ``.git/``, ``dist/``, ``build/``,
    ``target/``) — even if it otherwise looks plan-shaped.
    """
    if not isinstance(path, str) or not path:
        return False
    path = path.replace("\\", "/")
    if _EXCLUDED_PATH_RE.search(path):
        return False
    if _PLAN_DIR_RE.search(path):
        return True
    base = basename(path).lower()
    if not base:
        return False
    if base in _STANDARD_REPO_FILES:
        return False
    # Basename matching: require a text-like extension to avoid hitting
    # ``planner.py`` etc.
    if not (base.endswith(".md") or base.endswith(".txt")):
        return False
    return bool(_PLAN_BASENAME_RE.search(base))


# ---------------------------------------------------------------------------
# Structured-prompt classification (outline § Common definitions).
# ---------------------------------------------------------------------------

_BULLET_RE = re.compile(r"^[ \t]*([-*]\s|\d+\.\s)", re.MULTILINE)

# Distinct sequence words. Order doesn't matter; uniqueness does.
_SEQUENCE_WORDS: frozenset[str] = frozenset(
    {"step", "phase", "first", "then", "next", "finally"}
)
_SEQUENCE_WORD_RE = re.compile(
    r"\b(step|phase|first|then|next|finally)\b", re.IGNORECASE
)

STRUCTURED_PROMPT_TOKEN_FLOOR = 200


def is_structured_prompt(text: str, token_count: int) -> bool:
    """Apply the outline's structured-first-prompt rule.

    Returns True iff ``token_count > 200`` AND (
        ≥2 markdown list lines matching ``^[-*]\\s`` or ``^\\d+\\.\\s``,
        OR ≥2 distinct sequence words from
        ``{step, phase, first, then, next, finally}``).
    """
    if token_count <= STRUCTURED_PROMPT_TOKEN_FLOOR:
        return False
    if not text:
        return False
    if len(_BULLET_RE.findall(text)) >= 2:
        return True
    distinct = {m.group(1).lower() for m in _SEQUENCE_WORD_RE.finditer(text)}
    return len(distinct & _SEQUENCE_WORDS) >= 2


# ---------------------------------------------------------------------------
# Plan signal detector.
# ---------------------------------------------------------------------------

# Skill names that fire a strong plan signal. We accept both bare and
# ``superpowers:``-prefixed names because Claude Code skills can be
# invoked either way.
_PLAN_SKILL_NAMES: frozenset[str] = frozenset(
    {"writing-plans", "superpowers:writing-plans"}
)
_BRAINSTORM_SKILL_NAMES: frozenset[str] = frozenset(
    {"brainstorming", "superpowers:brainstorming"}
)

# Workflow/governance skills are structured planning context even when the
# work is not expressed through a literal plan file. Keep this as semantic
# skill-name matching, not prompt/content inspection.
_WORKFLOW_SKILL_TERMS: tuple[str, ...] = (
    "orchestrator",
    "evaluation",
    "evaluator",
    "judge",
    "judging",
    "quality-review",
    "readiness-repair",
    "repair",
    "scaffold",
    "skill-creator",
    "document-assembly",
    "source-ledger",
    "known-unknowns",
)
WORKFLOW_SKILL_EARLY_SIGNAL = "workflow_skill_early"
WORKFLOW_SKILL_SIGNAL = "workflow_skill"

# Cap for "first N tool calls" rules (per outline).
TODOWRITE_TOOL_CALL_WINDOW = 10
PLAN_MD_READ_TOOL_CALL_WINDOW = 5
TODOWRITE_MIN_ITEMS = 3


@dataclass(frozen=True)
class PlanSignals:
    """Plan-signal detection result.

    ``strong`` and ``weak`` are tuples of fixed categorical signal names
    (NEVER raw file paths or prompt text), safe to cross the wire.

    ``is_planned`` matches the outline rule: ≥1 strong OR ≥2 weak.
    """

    strong: tuple[str, ...]
    weak: tuple[str, ...]
    is_planned: bool


def _skill_names_for_event(e: Event) -> tuple[str, ...]:
    names: list[str] = []
    if e.skill_name:
        names.append(e.skill_name)
    raw = getattr(e, "raw_input", None)
    if isinstance(raw, dict):
        raw_names = raw.get("skill_names")
        if isinstance(raw_names, (list, tuple)):
            names.extend(n for n in raw_names if isinstance(n, str) and n)
    return tuple(dict.fromkeys(names))


def is_workflow_plan_skill(name: str | None) -> bool:
    """Return True for skills that encode workflow, judging, or governance.

    This intentionally does not treat every skill invocation as planning. A
    drafting or formatting skill may be execution-only; scaffold/evaluator/
    judge/orchestrator/repair skills are stronger evidence that the session is
    operating under a structured workflow.
    """
    if not name:
        return False
    normalized = name.lower().replace("_", "-")
    return any(term in normalized for term in _WORKFLOW_SKILL_TERMS)


def detect_plan_signals(
    events: list[Event],
    *,
    project_had_plan_artifact_prior_24h: bool = False,
) -> PlanSignals:
    """Detect plan signals on a single session's events.

    Reads only structural flags (``is_structured_prompt``,
    ``is_plan_file_write``, ``is_plan_md_read``, ``skill_name``,
    ``todo_count``, ``tool_name``) precomputed by ``events.read_events``.
    Never inspects raw prompt or tool input strings.
    """
    tool_events = [e for e in events if e.kind == EventKind.ASSISTANT_TOOL]
    first_n_tools = tool_events[:TODOWRITE_TOOL_CALL_WINDOW]
    first_5_tools = tool_events[:PLAN_MD_READ_TOOL_CALL_WINDOW]

    strong: list[str] = []

    # 1. EnterPlanMode tool used anywhere in the session.
    if any(e.tool_name == "EnterPlanMode" for e in tool_events):
        strong.append("EnterPlanMode")

    # 1b. Codex ``update_plan`` tool call — the Codex structured-plan
    # affordance (steps with statuses). A strong plan signal, the rough
    # analog of Claude's TodoWrite/EnterPlanMode. The categorical signal
    # name stays provider-neutral.
    if any(e.tool_name == "update_plan" for e in tool_events):
        strong.append("update_plan")

    # 2 & 3. /writing-plans and /brainstorming skill invocations.
    seen_writing_plans = False
    seen_brainstorming = False
    for e in tool_events:
        if e.skill_name and e.skill_name in _PLAN_SKILL_NAMES:
            seen_writing_plans = True
        if e.skill_name and e.skill_name in _BRAINSTORM_SKILL_NAMES:
            seen_brainstorming = True
    if seen_writing_plans:
        strong.append("/writing-plans skill")
    if seen_brainstorming:
        strong.append("/brainstorming skill")

    # 3b. Structured workflow / governance skills. Early use is a strong
    # plan signal; later use is weak evidence that can combine with a prior
    # project plan artifact.
    workflow_skill_seen = any(
        is_workflow_plan_skill(name)
        for e in tool_events
        for name in _skill_names_for_event(e)
    )
    workflow_skill_early = any(
        is_workflow_plan_skill(name)
        for e in first_n_tools
        for name in _skill_names_for_event(e)
    )
    if workflow_skill_early:
        strong.append(WORKFLOW_SKILL_EARLY_SIGNAL)

    # 4. TodoWrite with ≥3 items in the first 10 tool calls.
    for e in first_n_tools:
        if e.tool_name == "TodoWrite" and e.todo_count >= TODOWRITE_MIN_ITEMS:
            strong.append("TodoWrite>=3")
            break

    # 5. Plan-shaped file written this session.
    if any(e.is_plan_file_write for e in tool_events):
        strong.append("plan_file_write")

    # ---- Weak signals ----
    weak: list[str] = []

    # 1. Structured first prompt. Only the session's first USER event counts.
    first_user = next((e for e in events if e.kind == EventKind.USER), None)
    if first_user is not None and first_user.is_structured_prompt:
        weak.append("structured_first_prompt")

    # 2. Plan-shaped .md Read in first 5 tool calls.
    if any(e.tool_name == "Read" and e.is_plan_md_read for e in first_5_tools):
        weak.append("plan_md_read_early")

    # 3. Prior 24h plan artifact in the same project (caller-supplied).
    if project_had_plan_artifact_prior_24h:
        weak.append("prior_24h_plan_artifact")

    # 4. Workflow skill appeared, but not early enough to be a strong signal.
    if workflow_skill_seen and not workflow_skill_early:
        weak.append(WORKFLOW_SKILL_SIGNAL)

    is_planned = len(strong) >= 1 or len(weak) >= 2
    return PlanSignals(strong=tuple(strong), weak=tuple(weak), is_planned=is_planned)


def session_produced_plan_artifact(events: list[Event]) -> bool:
    """Did this session produce a plan artifact (outline definition)?

    Used by the scanner to compute the prior-24h cross-session lookback
    signal. A plan artifact is any of:
      * ``EnterPlanMode`` tool call,
      * ``writing-plans`` skill invocation,
      * ``brainstorming`` skill invocation,
      * early workflow/governance skill invocation,
      * ``TodoWrite`` with ≥3 items in the first 10 tool calls,
      * a file written to a plan-shaped ``.md`` path.

    This is exactly the union of the strong-signal triggers, so we
    reuse ``detect_plan_signals`` and check ``strong != ()``.
    """
    sig = detect_plan_signals(events, project_had_plan_artifact_prior_24h=False)
    return len(sig.strong) > 0


__all__ = [
    "PLAN_MD_READ_TOOL_CALL_WINDOW",
    "PlanSignals",
    "STRUCTURED_PROMPT_TOKEN_FLOOR",
    "TODOWRITE_MIN_ITEMS",
    "TODOWRITE_TOOL_CALL_WINDOW",
    "WORKFLOW_SKILL_EARLY_SIGNAL",
    "WORKFLOW_SKILL_SIGNAL",
    "detect_plan_signals",
    "is_plan_shaped_path",
    "is_structured_prompt",
    "is_workflow_plan_skill",
    "session_produced_plan_artifact",
]
