"""Prompt similarity — Jaccard repetitive-prompt detector.

Anchors:
- ``plans/003_outline.md`` § "repetitive prompts" anti-pattern.
- ``plans/004_wave1_implementation.md`` § Task 7.2.

A *qualifying pair* is any pair of user messages whose distinct-nonstop
token count is >= ``MIN_TOKENS``. A *repetitive pair* is a qualifying
pair whose Jaccard similarity is >= ``THRESHOLD``.

Privacy: raw user text is consumed IN MEMORY only. The public result is
a counts-only dataclass (``RepetitiveCounts``) — never raw text, never
actual Jaccard floats, never token sets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Stopword list — common English connectives + chat fillers ("yes", "ok",
# "please", "go", "continue", "fix", "do", "done", "now", "next", "then").
# Kept conservative and tunable later (Task 11.1).
STOPWORDS: frozenset[str] = frozenset(
    """a about above after again against all am an and any are as at be
    because been before being below between both but by can did do does
    doing don during each few for from further had has have having he
    her here hers herself him himself his how i if in into is it its
    itself just me more most my myself no nor not now of off on once
    only or other our ours ourselves out over own same she should so
    some such t than that the their theirs them themselves then there
    these they this those through to too under until up very was we
    were what when where which while who whom why will with you your
    yours yourself yourselves yes ok please go continue fix do done
    now next then""".split()
)

# Pulls alphabetic word tokens of length >= 2 from a free-text string.
_TOKEN_RE = re.compile(r"\b[a-zA-Z]{2,}\b")

# Minimum number of distinct nonstop tokens for a user message to be
# considered "long enough" to participate in similarity comparison.
MIN_TOKENS = 50

# Jaccard similarity threshold above which a qualifying pair is flagged
# as repetitive. Tunable via Task 11.1.
THRESHOLD = 0.6


def tokenize_nonstop(text: str) -> set[str]:
    """Return the set of distinct lowercase nonstop tokens in ``text``.

    Tokens: alphabetic substrings of length >= 2. Numbers, punctuation,
    and stopwords are dropped. Returns ``set()`` for empty input.
    """
    if not text:
        return set()
    toks = _TOKEN_RE.findall(text.lower())
    return {t for t in toks if t not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets. Empty-vs-empty → 0.0 (avoid
    divide-by-zero); otherwise ``|a ∩ b| / |a ∪ b|``.
    """
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass(frozen=True)
class RepetitiveCounts:
    """Counts-only result. Never carries raw text or float similarities."""

    qualifying_pairs: int
    repetitive_pairs: int


def jaccard_repetitive_rate(user_texts: list[str]) -> RepetitiveCounts:
    """Compute (qualifying_pairs, repetitive_pairs) over ``user_texts``.

    Privacy: the input strings are tokenized and discarded; only counts
    cross the function boundary. The returned dataclass has only integer
    fields.

    A pair (i, j) with i < j is *qualifying* iff both ``tokenize_nonstop``
    sets meet ``MIN_TOKENS`` distinct tokens. A qualifying pair is
    *repetitive* iff its Jaccard similarity is >= ``THRESHOLD``.
    """
    tokens_list: list[set[str]] = []
    for text in user_texts:
        toks = tokenize_nonstop(text or "")
        if len(toks) >= MIN_TOKENS:
            tokens_list.append(toks)

    qualifying = 0
    repetitive = 0
    for i in range(len(tokens_list)):
        for j in range(i + 1, len(tokens_list)):
            qualifying += 1
            if jaccard(tokens_list[i], tokens_list[j]) >= THRESHOLD:
                repetitive += 1
    return RepetitiveCounts(
        qualifying_pairs=qualifying, repetitive_pairs=repetitive
    )


__all__ = [
    "MIN_TOKENS",
    "RepetitiveCounts",
    "STOPWORDS",
    "THRESHOLD",
    "jaccard",
    "jaccard_repetitive_rate",
    "tokenize_nonstop",
]
