#!/usr/bin/env python3
"""Read-only real-data validation for Time Accounting v2 (plan 034 Task 10; plan 036).

Usage: python3 -I tools/validate_time_accounting_v2.py <claude_projects_dir> [days=30]

Builds each session in process through the scanner's own read/score
functions (never the upload, config or feature-cache paths) and prints
before (legacy 0.7.14 fields) vs after (``time_accounting``) aggregates.
Claude sessions give the headline; Codex and Cursor get one count line
each. Prints numbers and session ids only. Writes nothing.
"""
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # -I drops the script dir; keep imports local.


def _legacy_lev(num, den):
    return num / den if den else float("nan")


def validate(agent, cutoff_ms, label):
    from scripts import scanner as sc
    from scripts.time_accounting import identity_violations

    metas = [m for m in agent.find_sessions() if m.last_ts_ms >= cutoff_ms]
    out = []
    errors = Counter()
    none = 0
    for m in metas:
        try:
            ev, tm, tc = sc._read_session(agent, m)
            ps = sc._build_per_session(m, agent.agent_id, ev, tm, tc, {})
        except Exception as e:  # count, never print messages (may hold text)
            errors[type(e).__name__] += 1
            continue
        if ps.time_accounting is None:
            none += 1
            continue
        out.append((m.session_id, ps, ps.time_accounting))
    return metas, out, none, errors


def main():
    projects = Path(sys.argv[1]).expanduser().resolve()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    # Point provider discovery at the given tree; Codex/Cursor use their own
    # default homes (read only).
    os.environ["CONDUCTORSCORE_CLAUDE_HOME"] = str(projects.parent)
    from scripts.agents.claude import ClaudeAdapter
    from scripts.time_accounting import identity_violations

    t0 = time.time()
    cutoff = int((time.time() - days * 86400) * 1000)
    metas, rows, none, errors = validate(ClaudeAdapter(), cutoff, "claude")

    S = lambda f: sum(f(ps, ta) for _, ps, ta in rows)
    h = S(lambda ps, ta: ps.hitl_minutes)
    afk = S(lambda ps, ta: ps.afk_minutes)
    disp = S(lambda ps, ta: ps.afk_dispatch_minutes)
    par = S(lambda ps, ta: ps.afk_parallel_minutes_foreground)
    bg_before = afk - disp
    ia = S(lambda ps, ta: ta.interactive_read_write_minutes + ta.interactive_model_minutes
           + ta.interactive_tool_minutes)
    bg = S(lambda ps, ta: ta.background_model_minutes + ta.background_tool_minutes)
    am = S(lambda ps, ta: ta.agents_model_minutes)
    at = S(lambda ps, ta: ta.agents_tool_minutes)
    union = S(lambda ps, ta: ta.agents_union_minutes)
    wall = S(lambda ps, ta: ta.wall_clock_minutes)

    print("Claude sessions in last %dd: discovered=%d validated=%d no_ta=%d errors=%s"
          % (days, len(metas), len(rows), none, dict(errors) or 0))
    print("wall clock minutes (sum): %d" % wall)
    print()
    print("%-34s %12s %12s" % ("quantity (minutes unless noted)", "before", "after"))
    rw = S(lambda ps, ta: ta.interactive_read_write_minutes)
    print("%-34s %12d %12d" % ("Interactive", h, ia))
    print("%-34s %12s %12d" % ("  reading+typing (R5 credit)", "-", rw))
    print("%-34s %12s %12d" % ("  model + tool", "-", ia - rw))
    print("%-34s %12d %12d" % ("Background main thread", bg_before, bg))
    print("%-34s %12d %12d" % ("Agent time (sum)", par, am + at))
    print("%-34s %12s %12d" % ("  agents model / tool", "-", am))
    print("%-34s %12s %12d" % ("", "-", at))
    print("%-34s %12s %12d" % ("Agent union", "-", union))
    mx = max([ta.agents_max_concurrent for _, _, ta in rows] or [0])
    ct = [ta.agents_max_concurrent for _, _, ta in rows if ta.agents_count]
    print("%-34s %12s %12d" % ("Max concurrent agents (max)", "-", mx))
    print("%-34s %12s %12.1f" % ("Max concurrent (mean, w/ agents)", "-",
                                  statistics.mean(ct) if ct else 0))
    print("%-34s %12.2f %12.2f" % ("Leverage (aggregate, x)",
                                    _legacy_lev(bg_before + par, h),
                                    _legacy_lev(bg + am + at, ia)))
    print("%-34s %12s %12.2f" % ("Leverage excl. R5 (aggregate, x)", "-",
                                  _legacy_lev(bg + am + at, ia - rw)))
    for nm, fn in (("new excl. R5", lambda ps, ta: _legacy_lev(
                       ta.background_model_minutes + ta.background_tool_minutes
                       + ta.agents_model_minutes + ta.agents_tool_minutes,
                       ta.interactive_model_minutes + ta.interactive_tool_minutes)),
                   ("legacy", lambda ps, ta: _legacy_lev(ps.afk_minutes - ps.afk_dispatch_minutes
                    + ps.afk_parallel_minutes_foreground, ps.hitl_minutes)),
                   ("new", lambda ps, ta: _legacy_lev(
                       ta.background_model_minutes + ta.background_tool_minutes
                       + ta.agents_model_minutes + ta.agents_tool_minutes,
                       ta.interactive_read_write_minutes + ta.interactive_model_minutes
                       + ta.interactive_tool_minutes))):
        vals = [fn(ps, ta) for _, ps, ta in rows]
        vals = [v for v in vals if v == v]
        print("  per-session %s leverage: n=%d median=%.2f mean=%.2f" % (
            nm, len(vals), statistics.median(vals) if vals else 0,
            statistics.mean(vals) if vals else 0))
    lr_b = [ps.afk_max_streak_minutes for _, ps, _ in rows]
    lr_a = [ta.longest_run_minutes for _, _, ta in rows]
    print("%-34s %12d %12d" % ("Longest run (max)", max(lr_b or [0]), max(lr_a or [0])))
    print("%-34s %12.1f %12.1f" % ("Longest run (mean)",
                                    statistics.mean(lr_b or [0]), statistics.mean(lr_a or [0])))
    print()
    # Two rows, idle by reason (plan 036). Grid Interactive idle =
    # interactive.idle (in-turn) + between_turns.idle.
    bt_idle = S(lambda ps, ta: ta.between_turns_idle_minutes)
    bt_agents = S(lambda ps, ta: ta.between_turns_with_agents_minutes)
    ia_in = S(lambda ps, ta: ta.interactive_idle_minutes)
    print("Interactive idle (after): count / minutes  total=%d" % (ia_in + bt_idle))
    print("  %-30s %8d %10d" % ("between turns", S(lambda ps, ta: ta.between_turns_gaps), bt_idle))
    print("  %-30s %8s %10d" % ("  while your agents worked", "", bt_agents))
    print("  %-30s %8s %10d" % ("  nothing running", "", bt_idle - bt_agents))
    for r in ("permission_wait", "no_activity"):
        print("  %-30s %8d %10d" % (
            r, S(lambda ps, ta, r=r: getattr(ta, "interactive_%s_count" % r)),
            S(lambda ps, ta, r=r: getattr(ta, "interactive_%s_minutes" % r))))
    print("  (in-turn interactive.idle_minutes=%d)" % ia_in)
    print("  with_agents share of between turns: %.1f%% (%d / %d min)" % (
        100.0 * bt_agents / bt_idle if bt_idle else 0.0, bt_agents, bt_idle))
    print("Background idle (after): count / minutes  total=%d"
          % S(lambda ps, ta: ta.background_idle_minutes))
    for r in ("dispatch_wait", "tool_over_cap", "polling", "no_activity"):
        print("  %-30s %8d %10d" % (
            r, S(lambda ps, ta, r=r: getattr(ta, "background_%s_count" % r)),
            S(lambda ps, ta, r=r: getattr(ta, "background_%s_minutes" % r))))
    print("  agents idle=%d (not in the grid)" % S(lambda ps, ta: ta.agents_idle_minutes))
    print("  permission_signal: %s" % dict(Counter(ta.permission_signal for _, _, ta in rows)))
    print()
    bad = [(sid, identity_violations(ta)) for sid, _, ta in rows]
    bad = [(s, v) for s, v in bad if v]
    print("Identity violations: %d sessions of %d" % (len(bad), len(rows)))
    print("  violated identity names: %s" % dict(Counter(n for _, v in bad for n in v)))
    for sid, v in bad[:5]:
        print("  %s: %s" % (sid, v))
    print()
    for name, mod, cls in (("codex", "scripts.agents.codex", "CodexAdapter"),
                           ("cursor", "scripts.agents.cursor", "CursorAdapter")):
        try:
            adapter = getattr(__import__(mod, fromlist=[cls]), cls)()
            m2, r2, n2, e2 = validate(adapter, cutoff, name)
            v2 = sum(1 for _, _, ta in r2 if identity_violations(ta))
            print("%s: sessions=%d validated=%d no_ta=%d errors=%s identity_violation_sessions=%d"
                  % (name, len(m2), len(r2), n2, dict(e2) or 0, v2))
        except Exception as e:
            print("%s: unavailable (%s)" % (name, type(e).__name__))
    print("\nruntime_seconds=%.1f" % (time.time() - t0))


if __name__ == "__main__":
    main()
