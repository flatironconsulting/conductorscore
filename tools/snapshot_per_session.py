#!/usr/bin/env python3
"""Read-only oracle snapshot of the wire dict of each PerSession (plan 035 Task 0).

Usage: python3 -I tools/snapshot_per_session.py <claude_projects_dir> <count> <out.json> [must_include_prefix=66272d17]
       python3 -I tools/snapshot_per_session.py <claude_projects_dir> --ids-from <earlier.json> <out.json> [must_include_prefix]

Builds the ``count`` newest Claude sessions in process via the scanner's own
``_read_session`` + ``_build_per_session`` (no upload, no config, no feature
cache) and writes {session_id: the wire dict of each PerSession} with sorted keys.
``--ids-from`` builds exactly the session ids of an earlier snapshot instead
(ids no longer on disk are listed on stderr), so two runs compare the same set.

Next to ``out.json`` it writes ``out.stat.json``: {session_id: [[path, size,
mtime_ns], ...]} for the main transcript and each subagent transcript, or
null when they changed while this run built. Two snapshots compare fairly
only for sessions whose stat lists are equal and non-null (a session still
being written changes between runs).
The data holds real content: do not commit the output.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # -I drops the script dir; keep imports local.


def main():
    projects = Path(sys.argv[1]).expanduser().resolve()
    ids = None
    if sys.argv[2] == "--ids-from":
        ids = set(json.loads(Path(sys.argv[3]).read_text()))
        args = sys.argv[4:]
    else:
        count = int(sys.argv[2])
        args = sys.argv[3:]
    out = Path(args[0])
    must = args[1] if len(args) > 1 else "66272d17"
    os.environ["CONDUCTORSCORE_CLAUDE_HOME"] = str(projects.parent)
    from scripts import scanner as sc
    from scripts.agents.claude import ClaudeAdapter
    from scripts.agents.claude.discovery import subagent_transcripts
    from scripts.output_schema import DeviceMeta, ExtractorOutput

    # PerSession has no to_dict; the wire shape lives in ExtractorOutput.to_dict
    # (a fixed placeholder device keeps the wrapper deterministic).
    dev = DeviceMeta(device_id="snapshot", client_version="snapshot", extracted_at_ms=0)

    def wire(ps):
        return ExtractorOutput(device=dev, sessions=(ps,)).to_dict()["sessions"][0]

    agent = ClaudeAdapter()
    metas = sorted(agent.find_sessions(), key=lambda m: (-m.last_ts_ms, m.session_id))
    if ids is not None:
        chosen = [m for m in metas if m.session_id in ids]
        missing = sorted(ids - {m.session_id for m in chosen})
        if missing:
            print("missing ids: %s" % " ".join(missing), file=sys.stderr)
    else:
        chosen = metas[:count]
    if ids is None and not any(m.session_id.startswith(must) for m in chosen):
        extra = [m for m in metas if m.session_id.startswith(must)]
        if not extra:
            sys.exit("required session %s not found" % must)
        chosen = chosen[: count - 1] + extra[:1]

    def stat(p):
        st = p.stat()
        return [str(p), st.st_size, st.st_mtime_ns]

    def stats():
        return {
            m.session_id: [stat(m.jsonl_path)]
            + [stat(p) for p in subagent_transcripts(m.jsonl_path)]
            for m in chosen
            if m.jsonl_path is not None
        }

    pre = stats()
    result, times = {}, {}
    t0 = time.perf_counter()
    for m in chosen:
        t1 = time.perf_counter()
        ev, tm, tc = sc._read_session(agent, m)
        ps = sc._build_per_session(m, agent.agent_id, ev, tm, tc, {})
        times[m.session_id] = time.perf_counter() - t1
        result[m.session_id] = wire(ps)
    total = time.perf_counter() - t0

    out.parent.mkdir(parents=True, exist_ok=True)
    ordered = {k: result[k] for k in sorted(result)}
    out.write_text(json.dumps(ordered, sort_keys=True, indent=1, default=str))
    # A session whose files changed during the build keeps no stat entry
    # (None), so it never compares as stable.
    post = stats()
    stable = {k: (v if post.get(k) == v else None) for k, v in pre.items()}
    out.with_suffix(".stat.json").write_text(json.dumps(stable, sort_keys=True, indent=1))
    print("sessions=%d total_seconds=%.2f" % (len(result), total))
    for sid, t in times.items():
        if sid.startswith(must):
            print("%s seconds=%.2f" % (sid, t))


if __name__ == "__main__":
    main()
