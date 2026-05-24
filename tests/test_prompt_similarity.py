"""Tests for scripts.prompt_similarity — Jaccard repetitive-prompt detector.

Anchors: plans/003_outline.md § "repetitive prompts" anti-pattern.
plans/004_wave1_implementation.md § Task 7.2.

Privacy: this detector inspects user text IN MEMORY but emits only
integer counts. Raw text and actual Jaccard values must NEVER appear in
its public return value or any module-level state.
"""

from __future__ import annotations

from scripts.prompt_similarity import (
    MIN_TOKENS,
    RepetitiveCounts,
    THRESHOLD,
    jaccard,
    jaccard_repetitive_rate,
    tokenize_nonstop,
)


# ---------------------------------------------------------------------------
# tokenize_nonstop
# ---------------------------------------------------------------------------


def test_tokenize_lowercases_and_drops_stopwords():
    toks = tokenize_nonstop("The Quick Brown Fox is quick")
    # "the", "is" are stopwords; "quick" appears twice but set is unique.
    assert "the" not in toks
    assert "is" not in toks
    assert "quick" in toks
    assert "brown" in toks
    assert "fox" in toks


def test_tokenize_drops_one_letter_tokens():
    toks = tokenize_nonstop("a b c d hello")
    # Single-letter alphabetic tokens fail the {2,} regex
    assert toks == {"hello"}


def test_tokenize_strips_punctuation_and_numbers():
    toks = tokenize_nonstop("hello, world! 123 foo-bar 42")
    # Numbers are skipped (regex requires [a-zA-Z]); "foo-bar" splits to two.
    assert "hello" in toks
    assert "world" in toks
    assert "foo" in toks
    assert "bar" in toks
    assert "123" not in toks
    assert "42" not in toks


def test_tokenize_empty_or_none():
    assert tokenize_nonstop("") == set()


# ---------------------------------------------------------------------------
# jaccard
# ---------------------------------------------------------------------------


def test_jaccard_identical_is_one():
    s = {"a", "b", "c"}
    assert jaccard(s, s) == 1.0


def test_jaccard_disjoint_is_zero():
    assert jaccard({"a", "b"}, {"c", "d"}) == 0.0


def test_jaccard_partial_overlap():
    # {a, b, c} ∩ {b, c, d} = 2; ∪ = 4 ; 2/4 = 0.5
    assert jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 0.5


def test_jaccard_empty_both_is_zero():
    """Per spec: zero/zero collapses to 0.0 (avoid divide-by-zero)."""
    assert jaccard(set(), set()) == 0.0


# ---------------------------------------------------------------------------
# jaccard_repetitive_rate
# ---------------------------------------------------------------------------


_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _alpha_id(n: int) -> str:
    """3-letter alphabetic id for ``n`` in [0, 26^3). Used to build tokens
    that survive the alphabetic-only ``tokenize_nonstop`` regex.
    """
    a, b, c = n % 26, (n // 26) % 26, (n // 676) % 26
    return _LETTERS[c] + _LETTERS[b] + _LETTERS[a]


def _long_text(seed: str, n_unique: int = MIN_TOKENS + 5) -> str:
    """Build a non-stopword text with at least ``n_unique`` distinct tokens.

    Each token must survive ``tokenize_nonstop`` (alphabetic, len>=2, not a
    stopword). We use ``seed`` to scope tokens so two long_text calls with
    different seeds produce disjoint vocabularies.
    """
    return " ".join(f"{seed}word{_alpha_id(i)}" for i in range(n_unique))


def test_returns_counts_dataclass():
    out = jaccard_repetitive_rate([])
    assert isinstance(out, RepetitiveCounts)
    assert out.qualifying_pairs == 0
    assert out.repetitive_pairs == 0


def test_short_messages_excluded():
    """Texts under MIN_TOKENS distinct nonstop tokens never count as
    qualifying.
    """
    short = "hello world fox"
    out = jaccard_repetitive_rate([short, short, short])
    assert out.qualifying_pairs == 0
    assert out.repetitive_pairs == 0


def test_two_identical_long_messages_one_pair_one_repetitive():
    t = _long_text("alpha")
    out = jaccard_repetitive_rate([t, t])
    assert out.qualifying_pairs == 1
    assert out.repetitive_pairs == 1


def test_two_distinct_long_messages_one_pair_zero_repetitive():
    a = _long_text("alpha")
    b = _long_text("beta")
    out = jaccard_repetitive_rate([a, b])
    assert out.qualifying_pairs == 1
    assert out.repetitive_pairs == 0


def test_three_long_messages_two_same_third_distinct():
    a = _long_text("alpha")
    b = _long_text("beta")
    out = jaccard_repetitive_rate([a, a, b])
    # Pairs: (0,1) identical, (0,2) distinct, (1,2) distinct → 3 qualifying, 1 repetitive.
    assert out.qualifying_pairs == 3
    assert out.repetitive_pairs == 1


def test_mixed_short_and_long_messages():
    long_a = _long_text("alpha")
    long_b = _long_text("alpha")  # identical to long_a
    short = "yes please"
    out = jaccard_repetitive_rate([short, long_a, short, long_b, short])
    # Only the two long messages qualify; 1 pair, 1 repetitive.
    assert out.qualifying_pairs == 1
    assert out.repetitive_pairs == 1


def test_below_threshold_not_repetitive():
    """Two long messages with overlap below THRESHOLD are not repetitive."""
    # Construct sets with controlled overlap. Each has MIN_TOKENS+5 distinct
    # tokens. Share only ~30% — well below THRESHOLD=0.6.
    n = MIN_TOKENS + 5
    shared = [f"sharedword{_alpha_id(i)}" for i in range(int(n * 0.2))]
    a_only = [f"aonlyword{_alpha_id(i)}" for i in range(n - len(shared))]
    b_only = [f"bonlyword{_alpha_id(i)}" for i in range(n - len(shared))]
    a = " ".join(shared + a_only)
    b = " ".join(shared + b_only)
    out = jaccard_repetitive_rate([a, b])
    assert out.qualifying_pairs == 1
    assert out.repetitive_pairs == 0


# ---------------------------------------------------------------------------
# Privacy invariant — counts only, no raw text or Jaccard floats escape.
# ---------------------------------------------------------------------------


def test_repetitive_counts_has_only_integer_fields():
    """The result dataclass must expose only count integers — not raw text,
    not float Jaccard values, not token sets."""
    out = jaccard_repetitive_rate([_long_text("alpha"), _long_text("alpha")])
    # Inspect the dataclass: only int-typed counts are allowed.
    fields = [f for f in out.__dataclass_fields__.values()]  # type: ignore[attr-defined]
    for f in fields:
        assert f.type in ("int", int), (
            f"non-int field {f.name!r} of type {f.type!r} on RepetitiveCounts"
        )


def test_raw_text_not_present_in_repr():
    """The repr of the result must NOT contain the raw user text."""
    secret = "ULTRASECRET_DO_NOT_LEAK_42 " + _long_text("alpha")
    out = jaccard_repetitive_rate([secret, secret])
    assert "ULTRASECRET" not in repr(out)


def test_threshold_constant_is_tunable():
    """Public THRESHOLD constant exists (Task 11.1 will tune it)."""
    assert 0.0 <= THRESHOLD <= 1.0
