"""Hash-chain (plan 030, S4): the chain must change when any covered count is
edited, so a Tier-1 text-editor who bumps a number breaks it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "conductorscore" / "scripts"))

from output_schema import compute_session_chain  # noqa: E402


def _session(**over):
    base = {
        "session_hash": "a" * 16, "started_at_ms": 1, "ended_at_ms": 2,
        "total_lines_edited": 10, "commit_count": 1, "total_input_tokens": 5,
        "total_output_tokens": 5, "hitl_minutes": 1, "afk_minutes": 0, "idle_minutes": 0,
    }
    base.update(over)
    return base


def test_chain_is_16_lowercase_hex():
    chain = compute_session_chain([_session()])
    assert len(chain) == 16
    assert chain == chain.lower()
    assert all(c in "0123456789abcdef" for c in chain)


def test_chain_changes_when_a_count_is_edited():
    base = compute_session_chain([_session()])
    assert compute_session_chain([_session(total_lines_edited=99999)]) != base


def test_chain_changes_on_reorder():
    a, b = _session(session_hash="a" * 16), _session(session_hash="b" * 16)
    assert compute_session_chain([a, b]) != compute_session_chain([b, a])


def test_empty_corpus_is_empty_string():
    assert compute_session_chain([]) == ""
