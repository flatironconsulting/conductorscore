#!/usr/bin/env python3
"""Read-only recon for Time Accounting v2 (plan 034, Task 0).

Usage: python3 -I tools/recon_time_accounting_v2.py <projects_dir> [days=30]

Prints aggregate counts, distributions and value SHAPES only. Never prints
prompt text, commands, paths or tool output. Writes nothing.
"""
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DISPATCH = {"Agent", "Task", "Workflow"}
MACHINE_ORIGINS = {"sdk", "scheduled", "task_notification", "peer"}
NON_MESSAGE = {"queue-operation", "attachment", "file-history-snapshot", "summary",
               "system", "progress", "last-prompt", "permission-mode", "api-request-shape"}


def ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def dist(vals):
    if not vals:
        return "n=0"
    return "n=%d p50=%.0f p75=%.0f p90=%.0f p99=%.0f max=%.0f" % (
        len(vals), pct(vals, 50), pct(vals, 75), pct(vals, 90), pct(vals, 99), max(vals))


def shape(s):
    """Describe a string's shape without revealing it."""
    s = str(s)
    cls = []
    if re.search(r"[a-z]", s): cls.append("lower")
    if re.search(r"[A-Z]", s): cls.append("upper")
    if re.search(r"[0-9]", s): cls.append("digit")
    if re.search(r"[^A-Za-z0-9]", s): cls.append("punct")
    return "%d chars [%s]" % (len(s), "+".join(cls))


def blocks(d):
    c = (d.get("message") or {}).get("content")
    return c if isinstance(c, list) else []


def is_typed_prompt(d):
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
        return False
    if d.get("turnOrigin") in MACHINE_ORIGINS:
        return False
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        if any(b.get("type") == "tool_result" for b in c):
            return False
        text = "".join(b.get("text", "") for b in c if b.get("type") == "text")
        if not text:
            return False
    else:
        return False
    t = text.lstrip()
    return not (t.startswith("<task-notification>") or t.startswith("<system-reminder>"))


def read(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    return out


def main():
    root = Path(sys.argv[1]).expanduser()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    cutoff = time.time() - days * 86400
    mains, subs = [], []
    for p in root.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        (subs if "subagents" in p.parts else mains).append(p)

    gaps, gap_neg = [], 0
    gap_session_cnt = 0
    perm_total = Counter()           # (tool) -> user_ prompts
    perm_sources = Counter()
    perm_rows = []                   # (sid, tool, interval)
    cfg_rt_session = defaultdict(list)   # (sid, tool) -> config runtimes
    cfg_rt_global = defaultdict(list)    # tool -> config runtimes
    plan_src = Counter()
    ask_src = Counter()
    disp = Counter()                 # (where, tool, status)
    disp_block_main, disp_block_sub = [], []
    bg_use = {}                      # tool_use_id -> input keys
    bg_example = {}
    bg_total = 0
    notif_status = Counter()
    notif_fields = Counter()
    notif_id_shape = None
    par = Counter()
    asst_msgs = 0

    def scan(path, sid, where):
        nonlocal gap_neg, bg_total, notif_id_shape, asst_msgs
        evs = read(path)
        use = {}
        last_main = None
        seen_prompt = False
        msg_tools = defaultdict(int)
        for d in evs:
            t = d.get("type")
            tstamp = ts(d.get("timestamp", "")) if d.get("timestamp") else None
            if tstamp is not None and tstamp < cutoff:
                continue
            if where == "main" and t in ("user", "assistant") and not d.get("isSidechain"):
                if is_typed_prompt(d) and tstamp is not None:
                    if last_main is not None:
                        g = tstamp - last_main
                        if g < 0: gap_neg += 1
                        else: gaps.append(g)
                if tstamp is not None:
                    last_main = max(last_main or 0, tstamp)
            for b in blocks(d):
                bt = b.get("type")
                if bt == "tool_use":
                    use[b["id"]] = (b.get("name"), tstamp, b.get("input") or {})
                    mid = (d.get("message") or {}).get("id") or d.get("uuid") or id(d)
                    msg_tools[mid] += 1
                    inp = b.get("input") or {}
                    if b.get("name") == "Bash" and inp.get("run_in_background"):
                        bg_total += 1
                        bg_use[b["id"]] = sorted(inp.keys())
                elif bt == "tool_result":
                    u = use.get(b.get("tool_use_id"))
                    if not u:
                        continue
                    name, t0, inp = u
                    pd = d.get("permissionDecision")
                    src = pd.get("source") if isinstance(pd, dict) else None
                    interval = (tstamp - t0) if (tstamp and t0) else None
                    tur = d.get("toolUseResult")
                    if name == "ExitPlanMode":
                        plan_src[src or "(no permissionDecision)"] += 1
                    if name == "AskUserQuestion":
                        ask_src[src or "(no permissionDecision)"] += 1
                    if name not in ("ExitPlanMode", "AskUserQuestion") and src and interval is not None:
                        if src.startswith("user_"):
                            perm_sources[src] += 1
                            perm_total[name] += 1
                            perm_rows.append((sid, name, interval))
                        elif src == "config":
                            cfg_rt_session[(sid, name)].append(interval)
                            cfg_rt_global[name].append(interval)
                    if name in DISPATCH:
                        rc = b.get("content")
                        rtxt = rc if isinstance(rc, str) else " ".join(
                            x.get("text", "") for x in (rc or []) if isinstance(x, dict))
                        if isinstance(tur, dict) and tur.get("status"):
                            status = tur["status"]
                        elif rtxt.startswith("Async agent launched"):
                            status = "async_launched(by result text)"
                        elif b.get("is_error"):
                            status = "(error result)"
                        else:
                            status = "(no toolUseResult; sync by result text)"
                        disp[(where, name, status)] += 1
                        if not status.startswith("async_launched") and status != "(error result)" and interval is not None:
                            (disp_block_main if where == "main" else disp_block_sub).append(interval)
                    if b.get("tool_use_id") in bg_use and "result" not in bg_example:
                        txt = b.get("content")
                        txt = txt if isinstance(txt, str) else json.dumps(txt)
                        m = re.search(r"ID: (\S+?)\.", txt)
                        bg_example["result"] = {
                            "tool_use_input_keys": bg_use[b["tool_use_id"]],
                            "result_text_template": "Command running in background with ID: <id>. Output is being written to: <path>. ...",
                            "id_shape": shape(m.group(1)) if m else "n/a",
                            "toolUseResult_keys": sorted(tur.keys()) if isinstance(tur, dict) else type(tur).__name__,
                            "backgroundTaskId_shape": shape(tur["backgroundTaskId"]) if isinstance(tur, dict) and tur.get("backgroundTaskId") else "n/a",
                        }
            # notifications (string or text blocks)
            c = (d.get("message") or {}).get("content")
            texts = [c] if isinstance(c, str) else [b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text"]
            for tx in texts:
                if "<task-notification>" in tx:
                    for m in re.finditer(r"<task-notification>(.*?)</task-notification>", tx, re.S):
                        body = m.group(1)
                        for tag in re.findall(r"<([a-z-]+)>", body):
                            notif_fields[tag] += 1
                        s = re.search(r"<status>([^<]*)</status>", body)
                        if s: notif_status[s.group(1).strip()[:20]] += 1
                        i = re.search(r"<task-id>([^<]*)</task-id>", body)
                        if i and notif_id_shape is None:
                            notif_id_shape = shape(i.group(1).strip())
                        if d.get("turnOrigin"):
                            notif_fields["(turnOrigin=%s)" % d["turnOrigin"]] += 1
        for n in msg_tools.values():
            par[n] += 1

    for p in mains:
        scan(p, str(p), "main")
    for p in subs:
        scan(p, str(p.parent.parent), "subagent")

    print("== Scope ==")
    print("window_days=%d main_session_files=%d subagent_files=%d" % (days, len(mains), len(subs)))

    print("\n== 1. Pre-prompt gap (last main-thread message event -> next typed prompt), seconds ==")
    print(dist(gaps))
    if gaps:
        print("share >= 300s: %.1f%%   share >= 600s: %.1f%%   share == capped(>=300): n=%d" % (
            100 * sum(g >= 300 for g in gaps) / len(gaps), 100 * sum(g >= 600 for g in gaps) / len(gaps),
            sum(g >= 300 for g in gaps)))
        print("share < 30s: %.1f%%   p50..: see above" % (100 * sum(g < 30 for g in gaps) / len(gaps)))
        capped = [min(g, 300) for g in gaps]
        print("mean credit at cap 300: %.1fs" % statistics.mean(capped))
    print("negative gaps (queued/out-of-order, excluded): %d" % gap_neg)
    p90 = pct(gaps, 90)
    if p90 is not None:
        print("p90=%.1fs  p90_rounded_up_minute=%ds" % (p90, int(-(-p90 // 60) * 60)))

    print("\n== 2. Permission waits (source startswith user_, excl AskUserQuestion/ExitPlanMode) ==")
    print("sources:", dict(perm_sources))
    print("total prompts:", len(perm_rows))
    print("by tool (top 12):", perm_total.most_common(12))
    with_sess = with_glob = 0
    shares_s, shares_g = [], []
    for sid, name, iv in perm_rows:
        bs = cfg_rt_session.get((sid, name))
        bg = cfg_rt_global.get(name)
        if bs:
            with_sess += 1
            shares_s.append(min(1.0, statistics.median(bs) / iv) if iv > 0 else 1.0)
        if bg:
            with_glob += 1
            shares_g.append(min(1.0, statistics.median(bg) / iv) if iv > 0 else 1.0)
    n = len(perm_rows) or 1
    print("with same-session config baseline for tool: %d (%.1f%%)" % (with_sess, 100 * with_sess / n))
    print("with window-wide config baseline for tool:  %d (%.1f%%)" % (with_glob, 100 * with_glob / n))
    if shares_s: print("median share of interval removed (session baseline, among those with one): %.3f" % statistics.median(shares_s))
    if shares_g: print("median share of interval removed (window baseline, among those with one): %.3f" % statistics.median(shares_g))
    print("prompt interval seconds:", dist([iv for _, _, iv in perm_rows]))

    print("\n== 3. ExitPlanMode / AskUserQuestion permissionDecision.source ==")
    print("ExitPlanMode:", dict(plan_src))
    print("AskUserQuestion:", dict(ask_src))

    print("\n== 4. Dispatch tool results by (where, tool, toolUseResult.status) ==")
    for k, v in sorted(disp.items()):
        print(k, v)
    print("non-async dispatches main:     ", dist(disp_block_main))
    print("non-async dispatches subagents:", dist(disp_block_sub))

    print("\n== 5. run_in_background Bash ==")
    print("count of tool_use:", bg_total)
    print("example:", json.dumps(bg_example.get("result"), indent=1))
    print("task-notification inner tags:", dict(notif_fields))
    print("task-notification status values:", dict(notif_status))
    print("task-notification task-id shape:", notif_id_shape)

    print("\n== 6. tool_use blocks per assistant message ==")
    tot = sum(par.values()) or 1
    print("distribution:", dict(sorted(par.items())))
    print("share with >1: %.1f%%" % (100 * sum(v for k, v in par.items() if k > 1) / tot), "(messages with >=1 tool_use: %d)" % tot)


if __name__ == "__main__":
    main()
