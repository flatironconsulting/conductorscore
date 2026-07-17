"""Cursor IDE golden-scan scenario tests (Task 1.8).

Builds a synthetic Cursor global ``state.vscdb`` via
``tests/fixtures/cursor/builder.py``, runs the REAL scanner (``extract``),
and asserts exact scored ``PerSession`` metrics -- mirrors
``test_codex_golden_scan.py``'s numeric-rigor style: every expected value
below is computed BY HAND from the timestamps the scenario places, using
the actual threshold/formula each counter module documents (never
reverse-engineered from a failing run's output).

Two genuine reader bugs surfaced while deriving these scenarios by hand
and were fixed (not the tests bent to match buggy output):

1. ``scripts/edit_counter.py``'s ``EDIT_TOOLS`` set only recognized
   ``{"Edit", "Write", "MultiEdit", "apply_patch"}`` -- Cursor's reader
   canonicalizes its edit tool-call name to ``StrReplace``/``Write``/
   ``Delete`` (``scripts.agents.cursor.taxonomy.EDIT_TOOL_NAMES``), so
   ``StrReplace``/``Delete`` edits were silently invisible to
   ``files_modified``/``total_lines_edited``. Fixed by adding them to
   ``EDIT_TOOLS``.
2. ``scripts/agents/cursor/events.py`` never populated
   ``Event.cache_creation_input_tokens`` on ``ASSISTANT_TEXT`` events.
   ``scanner.py``'s ``tokens_by_model`` aggregator sums
   ``cache_creation_input_tokens``/``cache_input_tokens``/``output_tokens``
   (NOT ``input_tokens`` directly), so every Cursor session's input usage
   was silently dropped from ``tokens_by_model`` even though
   ``total_input_tokens`` (which sums ``input_tokens`` directly) was
   correct. Fixed by feeding ``input_tokens`` into
   ``cache_creation_input_tokens`` (Cursor reports no cache hit/miss
   split, so all real input is treated as a full-price MISS --
   ``cache_input_tokens`` stays 0).

Structural note on scenario (a): a Cursor bubble bundles a tool call AND
its result into ONE ``toolFormerData`` doc with a SINGLE ``createdAt`` --
the SQLite KV store only persists final state, so (unlike Claude/Codex)
there is no separate "call dispatched" vs. "result arrived" timestamp to
read a genuine call->result gap from
(``scripts.agents.cursor.events._tool_events`` gives both derived Events
the same ``timestamp_ms``, so ``turn_classifier._tool_runtime_intervals``
never produces a nonzero-width interval for a Cursor tool call). The AFK
turn below is therefore built the way Cursor's format actually allows a
long autonomous stretch to be recognized: several tool-call bubbles each
spaced <=5 minutes apart (each gap credited in full as "active" engaged
time per ``turn_classifier.K_TURN_SECONDS``), whose ACTIVE time sums past
the 5-minute HITL/AFK threshold.
"""
from __future__ import annotations

import hashlib

from scripts.scanner import extract
from tests.fixtures.cursor.builder import (
    MS,
    T0,
    assistant_bubble,
    tool_bubble,
    user_bubble,
    write_ide_store,
)
from tests.integration.test_extractor_integration import (
    cursor_home, _cursor_consent, _now_ms,  # noqa: F401  (cursor_home is a fixture)
)


def _scan(cursor_home, monkeypatch, composer: dict, now_ms: int):
    db = write_ide_store(cursor_home / "state.vscdb", [composer])
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(db))
    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=now_ms,
        consent_decision=_cursor_consent(),
    )
    assert out.sessions, "fixture fell outside the scan window"
    assert out.sessions[0].provider == "cursor"
    return out.sessions[0]


def _scan_dict(cursor_home, monkeypatch, composer: dict, now_ms: int) -> dict:
    """Like ``_scan`` but returns the serialized wire dict for the single
    scanned session (``ExtractorOutput.to_dict()``), for tests that need to
    assert a key's ABSENCE (byte-parity suppression), which the frozen
    ``PerSession`` dataclass attribute can't distinguish from ``False``."""
    db = write_ide_store(cursor_home / "state.vscdb", [composer])
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(db))
    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=now_ms,
        consent_decision=_cursor_consent(),
    )
    payload = out.to_dict()
    assert payload["sessions"], "fixture fell outside the scan window"
    assert payload["sessions"][0]["provider"] == "cursor"
    return payload["sessions"][0]


# ---------------------------------------------------------------------------
# (a) HITL/AFK/idle partition across a 30-minute session.
# ---------------------------------------------------------------------------


def test_hitl_afk_idle_partition(cursor_home, monkeypatch):
    """A 30-minute session with 3 turns:

    Turn A (HITL): user@0min -> assistant end_turn@1min.
      Single 1-minute gap, <=5min -> credited in full -> active=60s -> HITL.

    Turn B (AFK): user@2min opens the turn; three Shell tool-call bubbles
      at 6/10/14min (each a 4-minute gap from the previous event, <=5min
      so each is credited in FULL as active engaged time, even though it's
      a non-tool gap -- see the module docstring on why Cursor can't
      produce a genuine tool-runtime interval); the turn is closed by the
      NEXT user event at 29min ("next_user" turn-ender), whose 15-minute
      trailing gap (14min->29min) is > K_TURN_SECONDS (5min) and is a
      NON-tool gap (no interval covers it), so it is EXCLUDED entirely
      (0 credit) rather than capped.
        active = (6-2)+(10-6)+(14-10) minutes = 4+4+4 = 12min = 720s > 300s
        -> AFK. The trailing 14->29 gap contributes 0.

    Turn C (HITL): user@29min -> assistant end_turn@30min.
      Single 1-minute gap -> active=60s -> HITL.

    hitl_minutes = round((60+60)/60) = 2
    afk_minutes  = round(720/60) = 12
    window = [minute(0), minute(30)] -> window_minutes = 30
    idle_minutes = 30 - 2 - 12 = 16
    """
    composer_id = "cur-hitl-afk-idle"
    bubbles = [
        user_bubble("b1", "hi, please run the test suite and report back", T0),
        assistant_bubble("b2", "ok, starting now", T0 + 1 * MS),
        user_bubble("b3", "go ahead and iterate until it's green", T0 + 2 * MS),
        tool_bubble("b4", "Shell", {"command": "npm test"}, T0 + 6 * MS),
        tool_bubble("b5", "Shell", {"command": "npm test"}, T0 + 10 * MS),
        tool_bubble("b6", "Shell", {"command": "npm test"}, T0 + 14 * MS),
        user_bubble("b7", "looks good, wrap it up", T0 + 29 * MS),
        assistant_bubble("b8", "done, all green", T0 + 30 * MS),
    ]
    s = _scan(
        cursor_home,
        monkeypatch,
        {
            "composerId": composer_id,
            "createdAt": T0,
            "lastUpdatedAt": T0 + 30 * MS,
            "workspacePath": "/repo/hitl-afk",
            "bubbles": bubbles,
        },
        now_ms=T0 + 40 * MS,
    )

    assert s.hitl_minutes == 2
    assert s.afk_minutes == 12
    assert s.idle_minutes == 16


# ---------------------------------------------------------------------------
# (b) Denial counted -- feeds redundant_approvals_per_signature.
# ---------------------------------------------------------------------------


def test_denial_counted_in_redundant_approvals(cursor_home, monkeypatch):
    """A rejected tool call must surface in the ONE serialized metric that
    consumes ``Event.is_denied``: ``redundant_approvals_per_signature``
    (``scripts/approval_counter.py::count_redundant_approvals``, signal 1
    "Denials" in its module docstring). There is no separate cursor-only
    denial metric -- ``is_denied`` feeds exactly this dict, grouped by the
    same ``(Bash, <first token>)`` signature Claude/Codex Bash denials use.

    IMPORTANT: the Cursor rejection shape exercised here
    (``toolFormerData.userDecision == "rejected"``) is SYNTHETIC/UNVERIFIED
    -- per ``tests/fixtures/cursor/builder.py::tool_bubble`` and
    ``scripts/agents/cursor/events.py::_tool_events``, live recon never
    observed a rejection/denial ``toolFormerData`` shape (only "completed"/
    "error" statuses were seen in the one fully-populated composer
    examined). This test exercises the reader's denial-HANDLING path
    (``is_denied`` -> ``TOOL_RESULT.is_denied`` -> the approval counter),
    not a confirmed-real Cursor signal.

    Derivation: ``signature_for_bash("rm -rf /tmp/scratch")`` skips no
    env-assignment, takes the first token ``"rm"`` (not path-like: no "/"
    and doesn't start with "~"), giving signature ``("Bash", "rm")`` ->
    wire key ``"Bash::rm"``. The single denied dispatch is counted once by
    signal 1 (denials); signal 2 (approval-wait) is skipped for it because
    ``denied_dispatch`` already contains its ``tool_use_id`` (denial takes
    precedence -- module docstring's "at most once" rule).
    """
    composer_id = "cur-denial"
    bubbles = [
        user_bubble("b1", "please clean up the scratch dir", T0),
        tool_bubble(
            "b2", "Shell", {"command": "rm -rf /tmp/scratch"}, T0 + 1 * MS,
            user_decision="rejected",  # SYNTHETIC/UNVERIFIED -- see docstring above.
        ),
    ]
    s = _scan(
        cursor_home,
        monkeypatch,
        {
            "composerId": composer_id,
            "createdAt": T0,
            "lastUpdatedAt": T0 + 1 * MS,
            "workspacePath": "/repo/denial",
            "bubbles": bubbles,
        },
        now_ms=T0 + 10 * MS,
    )

    assert s.redundant_approvals_per_signature == {"Bash::rm": 1}


# ---------------------------------------------------------------------------
# (c) Edit session -- files_modified / total_lines_edited / significance.
# ---------------------------------------------------------------------------


def test_edit_session_significant(cursor_home, monkeypatch):
    """Two edit tool-call bubbles (StrReplace + Write) touching two
    distinct file_paths.

    StrReplace on /repo/src/foo.py: old_string="old" (1 non-empty line ->
      old_lines=1), new_string is 201 "\\n"-joined lines (200 newlines ->
      new_lines=201). ``edit_counter._line_count`` (via
      ``taxonomy.edit_footprint``) = max(1, 201, 1) = 201 lines.

    Write on /repo/src/bar.py: no old_string (old_lines=0), content is
      "l1\\nl2\\nl3\\nl4\\nl5" (4 newlines -> new_lines=5). Line count =
      max(0, 5, 1) = 5 lines.

    files_modified = 2 distinct hashed paths.
    total_lines_edited = 201 + 5 = 206.
    is_significant_edit_session: per edit_counter.py, files>5 (2 files:
      false) OR total_lines_edited>200 (206>200: TRUE) -> significant.

    This is scenario (c)'s exact reader-bug repro: before adding
    "StrReplace" to ``edit_counter.EDIT_TOOLS`` (see module docstring),
    the StrReplace bubble was invisible to this counter entirely, giving
    files_modified=1 / total_lines_edited=5 / is_significant=False.
    """
    composer_id = "cur-edit-session"
    new_lines_201 = "\n".join(f"line{i}" for i in range(201))
    bubbles = [
        user_bubble("b1", "refactor these two files", T0),
        tool_bubble(
            "b2", "StrReplace",
            {
                "file_path": "/repo/src/foo.py",
                "old_string": "old",
                "new_string": new_lines_201,
            },
            T0 + 1 * MS,
        ),
        tool_bubble(
            "b3", "Write",
            {"file_path": "/repo/src/bar.py", "content": "l1\nl2\nl3\nl4\nl5"},
            T0 + 2 * MS,
        ),
    ]
    s = _scan(
        cursor_home,
        monkeypatch,
        {
            "composerId": composer_id,
            "createdAt": T0,
            "lastUpdatedAt": T0 + 2 * MS,
            "workspacePath": "/repo/edit-session",
            "bubbles": bubbles,
        },
        now_ms=T0 + 10 * MS,
    )

    assert s.files_modified == 2
    assert s.total_lines_edited == 206
    assert s.total_lines_edited >= 6
    assert s.is_significant_edit_session is True


# ---------------------------------------------------------------------------
# (d) Plan signals -- TodoWrite drives the strong "TodoWrite>=3" signal.
# ---------------------------------------------------------------------------


def test_todo_write_drives_strong_plan_signal(cursor_home, monkeypatch):
    """A TodoWrite tool-call bubble with 3 todo items, as the session's
    only (hence first) tool call.

    Per ``scripts/plan_signals.py``: rule 4 ("TodoWrite with >=3 items in
    the first 10 tool calls", ``TODOWRITE_MIN_ITEMS=3``,
    ``TODOWRITE_TOOL_CALL_WINDOW=10``) fires the strong signal
    ``"TodoWrite>=3"``. No other strong/weak trigger is present (no
    EnterPlanMode/update_plan/plan skill/plan-shaped file write; the first
    user message is short, well under the 200-token structured-prompt
    floor; no plan-shaped .md Read). ``is_planned`` = (len(strong)>=1 or
    len(weak)>=2) = True via the strong branch alone.
    """
    composer_id = "cur-plan"
    bubbles = [
        user_bubble("b1", "let's get organized", T0),
        tool_bubble(
            "b2", "TodoWrite",
            {
                "todos": [
                    {"content": "task 1", "status": "pending"},
                    {"content": "task 2", "status": "pending"},
                    {"content": "task 3", "status": "pending"},
                ]
            },
            T0 + 1 * MS,
        ),
    ]
    s = _scan(
        cursor_home,
        monkeypatch,
        {
            "composerId": composer_id,
            "createdAt": T0,
            "lastUpdatedAt": T0 + 1 * MS,
            "workspacePath": "/repo/plan",
            "bubbles": bubbles,
        },
        now_ms=T0 + 10 * MS,
    )

    assert s.strong_plan_signals == ("TodoWrite>=3",)
    assert s.weak_plan_signals == ()
    assert s.is_planned is True


# ---------------------------------------------------------------------------
# (e) tokens_by_model keying across two distinct model ids.
# ---------------------------------------------------------------------------


def test_tokens_by_model_keyed_by_model_id(cursor_home, monkeypatch):
    """Three assistant-text bubbles with non-zero token usage: two under
    the real observed id ``"composer-2.5"``, one under a pass-through id
    ``"claude-4.5-sonnet"`` (Cursor lets the user pick a different
    backing model; the reader passes any non-"default" model string
    through unchanged -- ``events._model_or_none``).

    composer-2.5: (input=100,output=20) + (input=50,output=10)
      -> input sums to 150, output sums to 30.
    claude-4.5-sonnet: (input=30,output=5) -> input=30, output=5.

    ``scanner.py``'s ``tokens_by_model`` aggregator keys off
    ``cache_creation_input_tokens``/``cache_input_tokens``/
    ``output_tokens`` (NOT ``input_tokens`` directly) -- Cursor's reader
    now feeds ``input_tokens`` into ``cache_creation_input_tokens`` (see
    module docstring's reader-bug #2), so every real Cursor input token
    lands in the "input_miss" bucket; "input_hit" stays 0 (Cursor reports
    no cache hits).

    assistant_msgs_by_model dedupes on (timestamp_ms, model); all three
    bubbles have distinct timestamps, so each counts once:
    composer-2.5 -> 2, claude-4.5-sonnet -> 1.

    total_input_tokens = 100+50+30 = 180 (sums ``input_tokens`` directly,
    via ``count_compaction_and_tokens`` -- unaffected by the cache-split
    fix, confirms it wasn't already broken).
    total_output_tokens = 20+10+5 = 35.

    Fixtures deliberately use non-zero tokens throughout (unlike the
    Task 1.7 all-zero ``token_data_missing`` coverage, which this test
    does NOT duplicate) -- so ``token_data_missing`` must be suppressed
    entirely (not merely False) on the wire.
    """
    composer_id = "cur-tokens-by-model"
    bubbles = [
        user_bubble("b1", "hi", T0),
        assistant_bubble(
            "b2", "working on it", T0 + 1 * MS,
            model="composer-2.5", input_tokens=100, output_tokens=20,
        ),
        assistant_bubble(
            "b3", "almost done", T0 + 2 * MS,
            model="composer-2.5", input_tokens=50, output_tokens=10,
        ),
        assistant_bubble(
            "b4", "here's a pass-through-model reply", T0 + 3 * MS,
            model="claude-4.5-sonnet", input_tokens=30, output_tokens=5,
        ),
    ]
    composer = {
        "composerId": composer_id,
        "createdAt": T0,
        "lastUpdatedAt": T0 + 3 * MS,
        "workspacePath": "/repo/tokens",
        "bubbles": bubbles,
    }
    s = _scan(cursor_home, monkeypatch, composer, now_ms=T0 + 10 * MS)

    assert s.tokens_by_model == {
        "composer-2.5": {"input_miss": 150, "input_hit": 0, "output": 30},
        "claude-4.5-sonnet": {"input_miss": 30, "input_hit": 0, "output": 5},
    }
    assert s.assistant_msgs_by_model == {"composer-2.5": 2, "claude-4.5-sonnet": 1}
    assert s.total_input_tokens == 180
    assert s.total_output_tokens == 35
    assert s.token_data_missing is False

    # Byte-parity suppression: a session with real (non-zero) token data
    # must not emit the ``token_data_missing`` key at all, not merely
    # emit it as ``false`` -- a fresh cursor_home/monkeypatch pair since
    # pytest fixtures aren't re-enterable mid-test.
    wire = _scan_dict(cursor_home, monkeypatch, composer, now_ms=T0 + 10 * MS)
    assert "token_data_missing" not in wire


# ---------------------------------------------------------------------------
# (f) Task 2.2 golden — IDE + CLI sessions merge under one provider.
# ---------------------------------------------------------------------------


def test_cursor_ide_and_cli_sessions_merge_under_one_provider(
    cursor_home, monkeypatch
):
    """Both Cursor surfaces feeding ONE ``extract()`` run must merge into a
    single ``provider: "cursor"``: one session discovered from the IDE's
    ``state.vscdb`` and one from a CLI ``store.db``, two DISTINCT
    provider-namespaced ``session_hash`` values, ``providers_seen`` carrying
    ``"cursor"`` exactly once (not "cursor-ide" / "cursor-cli" — there is
    only one provider identity for both surfaces).

    The CLI session must also come out ``token_data_missing`` -- CLI
    ``store.db`` blobs carry no per-message token fields at all (§6/§7 of
    CURSOR_FORMAT.md), unlike the IDE surface which can carry real
    ``tokenCount`` data.
    """
    from tests.fixtures.cursor.cli_builder import (
        assistant_message as cli_assistant_message,
        user_message as cli_user_message,
        write_cli_store,
    )

    now = _now_ms()

    # --- IDE surface: one composer session. ---
    ide_composer_id = "cur-merge-ide"
    ide_db = write_ide_store(
        cursor_home / "state.vscdb",
        [
            {
                "composerId": ide_composer_id,
                "createdAt": now - 10 * MS,
                "lastUpdatedAt": now - 9 * MS,
                "workspacePath": "/repo/merge-ide",
                "bubbles": [
                    user_bubble("b1", "hi from the IDE", now - 10 * MS),
                    assistant_bubble("b2", "hello from the IDE", now - 9 * MS),
                ],
            }
        ],
    )
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(ide_db))

    # --- CLI surface: one store.db session (a distinct chats dir). ---
    cli_agent_id = "cur-merge-cli"
    chats_dir = cursor_home / "cli_chats"
    write_cli_store(
        chats_dir,
        {
            "agentId": cli_agent_id,
            "createdAt": now - 8 * MS,
            "messages": [
                cli_user_message("hi from the CLI", wrap_user_query=True),
                cli_assistant_message(text="hello from the CLI"),
            ],
        },
    )
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_CLI_DIR", str(chats_dir))

    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=now,
        consent_decision=_cursor_consent(),
    )
    payload = out.to_dict()

    sessions = payload["sessions"]
    assert len(sessions) == 2, sessions
    # One provider VALUE across both surfaces, not per-surface providers.
    assert {s["provider"] for s in sessions} == {"cursor"}
    assert "cursor" in payload["providers_seen"]

    ide_hash = hashlib.sha256(f"cursor:{ide_composer_id}".encode()).hexdigest()[:16]
    cli_hash = hashlib.sha256(f"cursor:{cli_agent_id}".encode()).hexdigest()[:16]
    hashes = {s["session_hash"] for s in sessions}
    assert len(hashes) == 2, "IDE and CLI sessions must hash to DISTINCT values"
    assert hashes == {ide_hash, cli_hash}

    by_hash = {s["session_hash"]: s for s in sessions}
    # CLI store.db carries no per-message token fields at all -- the CLI
    # session must be flagged missing (byte-parity: present and True, not
    # merely absent/False).
    assert by_hash[cli_hash].get("token_data_missing") is True
