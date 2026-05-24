"""Generate cross-language parity fixtures for the TS scorer.

Builds the four worked examples from ``plans/003_outline.md`` § "Worked
examples" by running the Python event pipeline (events.py →
session_window.py → minute_classifier.py) end-to-end, then packaging
the resulting per-session counts into the v0.7 wire envelope and
emitting one JSON file per example into
``server/web/tests/fixtures/parity/``.

The TS-side parity vitest (web/tests/parity.test.ts) loads each fixture
through ``ExtractorOutputSchema.parse`` then drives ``score()`` and
asserts the same numbers the Python ``test_worked_examples.py`` suite
locks in for the AFK leverage cluster — plus the Slice-5 (v0.7)
cache-aware token/plugin/builtin/dispatch rollups on example_2's
foreground session so ``costAggregate`` / ``pluginUsage`` /
``toolUsage`` / ``tokensAggregate`` get cross-language coverage too.

Run once after any change to the worked-examples fixtures or the wire
schema. Idempotent; overwrites in place.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow ``python3 scripts/generate_parity_fixtures.py`` from anywhere in
# the client tree.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.events import Event, EventKind  # noqa: E402
from scripts.minute_classifier import (  # noqa: E402
    MinuteBucket,
    afk_intervals,
    afk_max_streak_minutes,
    afk_parallel_minutes_foreground,
    classify_minutes,
    cron_intervals,
    cron_parallel_minutes,
)
from scripts.output_schema import (  # noqa: E402
    AfkInterval,
    ConfigCounts,
    DeviceMeta,
    ExtractorOutput,
    PerSession,
)
from scripts.session_window import compute_window  # noqa: E402


def _ms(m: int) -> int:
    return m * 60_000


def _user(session_id: str, minute: int) -> Event:
    return Event(
        kind=EventKind.USER,
        session_id=session_id,
        timestamp_ms=_ms(minute),
    )


def _main_tool(session_id: str, minute: int, tool: str) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name=tool,
        is_sidechain=False,
    )


def _sub_tool(
    session_id: str, minute: int, subagent_id: str, tool: str = "Read"
) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name=tool,
        is_sidechain=True,
        subagent_id=subagent_id,
    )


def _cron(session_id: str, minute: int) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name="ScheduleWakeup",
        is_sidechain=False,
    )


def _foreground_session_with_subagents(
    session_id: str,
    user_minute: int,
    start_active_minute: int,
    end_active_minute_inclusive: int,
    n_subagents: int,
) -> list[Event]:
    events: list[Event] = [_user(session_id, user_minute)]
    for _ in range(n_subagents):
        events.append(_main_tool(session_id, user_minute, "Task"))
    for m in range(start_active_minute, end_active_minute_inclusive + 1):
        for i in range(n_subagents):
            events.append(_sub_tool(session_id, m, f"sub-{i + 1}"))
    return events


# ---------------------------------------------------------------------------
# Fixture builders — each returns a list of session-event lists. Per-session
# event groups model what extractor.py would see after `find_sessions()`.
# ---------------------------------------------------------------------------


def example_1_session_groups() -> list[list[Event]]:
    return [
        _foreground_session_with_subagents(
            session_id="ex1",
            user_minute=9 * 60 + 5,
            start_active_minute=9 * 60 + 5,
            end_active_minute_inclusive=9 * 60 + 30,
            n_subagents=4,
        )
    ]


def example_2_session_groups() -> list[list[Event]]:
    fg = _foreground_session_with_subagents(
        session_id="ex2-fg",
        user_minute=9 * 60 + 5,
        start_active_minute=9 * 60 + 5,
        end_active_minute_inclusive=9 * 60 + 30,
        n_subagents=4,
    ) + _foreground_session_with_subagents(
        session_id="ex2-fg",
        user_minute=11 * 60,
        start_active_minute=11 * 60,
        end_active_minute_inclusive=11 * 60 + 15,
        n_subagents=1,
    )
    cron = [_cron("ex2-cron", 12 * 60 + i) for i in range(10)]
    return [fg, cron]


def example_3_session_groups() -> list[list[Event]]:
    events: list[Event] = [_user("ex3", 9 * 60)]
    for m in range(9 * 60, 9 * 60 + 31):
        events.append(_main_tool("ex3", m, "Read"))
    return [events]


def example_4_session_groups() -> list[list[Event]]:
    groups: list[list[Event]] = []
    minutes_per_day = 24 * 60
    for day in range(30):
        day_start = day * minutes_per_day
        groups.append(
            [_cron(f"ex4-cron-day-{day}", day_start + i) for i in range(15)]
        )
    return groups


# ---------------------------------------------------------------------------
# Compile a session-event list into a PerSession dataclass — same logic
# extractor.py runs at upload time, distilled to just the AFK leverage +
# session_hash fields the parity test cares about. Other fields are left
# at their defaults; the TS scorer ignores them when computing the
# metrics under test.
# ---------------------------------------------------------------------------


def _hex16(label: str) -> str:
    """Stable 16-hex-char ID for the wire-format hash regex."""
    import hashlib

    return hashlib.sha256(label.encode()).hexdigest()[:16]


def _build_per_session(
    events: list[Event],
    label: str,
    v07_overrides: dict | None = None,
) -> PerSession:
    window = compute_window(events)
    hitl_minutes = afk_minutes = idle_minutes = 0
    afk_parallel_fg = 0
    afk_max_streak = 0
    intervals: list[AfkInterval] = []
    if window is not None:
        buckets = classify_minutes(events, window)
        for _, b in buckets.items():
            if b == MinuteBucket.HITL:
                hitl_minutes += 1
            elif b == MinuteBucket.AFK:
                afk_minutes += 1
            else:
                idle_minutes += 1
        afk_parallel_fg = afk_parallel_minutes_foreground(events, buckets)
        afk_max_streak = afk_max_streak_minutes(buckets)
        for start, end_excl in afk_intervals(buckets):
            intervals.append(
                AfkInterval(
                    start_minute=start,
                    end_minute_exclusive=end_excl,
                    is_cron=False,
                )
            )
    cron_parallel = cron_parallel_minutes(events)
    for start, end_excl in cron_intervals(events):
        intervals.append(
            AfkInterval(
                start_minute=start,
                end_minute_exclusive=end_excl,
                is_cron=True,
            )
        )

    # started_at_ms / ended_at_ms must be > 0; use the first/last event
    # in the session's event list (Cron-only sessions have no window but
    # still produce events).
    started_at_ms = min(e.timestamp_ms for e in events)
    ended_at_ms = max(e.timestamp_ms for e in events)
    if started_at_ms <= 0:
        started_at_ms = 1
        ended_at_ms = max(ended_at_ms, started_at_ms + 1)

    overrides = v07_overrides or {}
    return PerSession(
        session_hash=_hex16(f"sess-{label}"),
        project_hash=_hex16(f"proj-{label}"),
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        hitl_minutes=hitl_minutes,
        afk_minutes=afk_minutes,
        idle_minutes=idle_minutes,
        afk_parallel_minutes_foreground=afk_parallel_fg,
        cron_parallel_minutes=cron_parallel,
        afk_max_streak_minutes=afk_max_streak,
        afk_intervals=tuple(intervals),
        # v0.6 — fluency surface (needed for the proportional per-model
        # token split that drives `costAggregate` / `tokensByModelDetailed`).
        assistant_msgs_by_model=overrides.get("assistant_msgs_by_model", {}),
        # v0.7 — cache-aware token split + plugins + builtin + dispatch.
        total_input_tokens=overrides.get("total_input_tokens", 0),
        total_output_tokens=overrides.get("total_output_tokens", 0),
        cache_input_tokens=overrides.get("cache_input_tokens", 0),
        cache_creation_input_tokens=overrides.get(
            "cache_creation_input_tokens", 0
        ),
        builtin_tool_invocations=overrides.get("builtin_tool_invocations", 0),
        plugin_invocations=overrides.get("plugin_invocations", 0),
        distinct_plugins=tuple(overrides.get("distinct_plugins", ())),
        agent_dispatches=overrides.get("agent_dispatches", 0),
    )


def _build_envelope(
    label: str,
    session_groups: list[list[Event]],
    *,
    session_overrides: dict[int, dict] | None = None,
    config: ConfigCounts | None = None,
) -> dict:
    overrides_by_idx = session_overrides or {}
    sessions = tuple(
        _build_per_session(
            events, f"{label}-{i}", v07_overrides=overrides_by_idx.get(i)
        )
        for i, events in enumerate(session_groups)
    )
    envelope = ExtractorOutput(
        device=DeviceMeta(
            device_id="00000000-0000-4000-8000-000000000000",
            client_version="0.1.0-parity",
            extracted_at_ms=0,
        ),
        config=config or ConfigCounts(),
        sessions=sessions,
    )
    return envelope.to_dict()


# Plugins used in example_2's fg session — pre-hashed so the parity test
# can assert `distinctPluginsUnion` length deterministically without
# pulling in hashlib on the TS side.
_EXAMPLE_2_PLUGINS = (
    "0123456789abcdef",
    "fedcba9876543210",
    "abcdef0123456789",
)

# Example 2 v0.7 overrides — applied to the foreground session (index 0).
# Picked to be stable, non-zero, and exercise:
#   • proportional per-model token split (2 models: opus + sonnet)
#   • cache hit vs miss split (75% miss / 25% hit ratio)
#   • plugin + builtin + dispatch invocation counters
# tokensAggregate sums across sessions — example_2 has 2 sessions but
# only the fg one carries token activity, so the sums are exactly what
# we inject below.
_EXAMPLE_2_FG_V07 = {
    "assistant_msgs_by_model": {
        "claude-opus-4-7": 60,
        "claude-sonnet-4-5": 40,
    },
    "total_input_tokens": 1_000_000,
    "total_output_tokens": 200_000,
    "cache_input_tokens": 250_000,
    "cache_creation_input_tokens": 750_000,
    "builtin_tool_invocations": 42,
    "plugin_invocations": 9,
    "distinct_plugins": _EXAMPLE_2_PLUGINS,
    "agent_dispatches": 4,
}


CASES: list[tuple[str, "callable", dict | None, ConfigCounts | None]] = [
    ("example_1", example_1_session_groups, None, None),
    (
        "example_2",
        example_2_session_groups,
        {0: _EXAMPLE_2_FG_V07},
        # Two installed plugins; one matches a used plugin, one doesn't —
        # the parity test only asserts `installedPluginsCount` through
        # `config.plugin_count`, so the identifiers are cosmetic.
        ConfigCounts(
            plugin_count=2,
            distinct_installed_plugins=(
                "0123456789abcdef",
                "1111222233334444",
            ),
        ),
    ),
    ("example_3", example_3_session_groups, None, None),
    ("example_4", example_4_session_groups, None, None),
]


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for label, groups_fn, session_overrides, config in CASES:
        payload = _build_envelope(
            label,
            groups_fn(),
            session_overrides=session_overrides,
            config=config,
        )
        out_path = out_dir / f"{label}.json"
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"wrote {out_path}")


if __name__ == "__main__":
    default = (
        Path(__file__).resolve().parent.parent.parent
        / "server"
        / "web"
        / "tests"
        / "fixtures"
        / "parity"
    )
    target = (
        Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default
    )
    main(target)
