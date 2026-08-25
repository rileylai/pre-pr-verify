"""Bounded presentation relevance for discovered requirement candidates.

This module deliberately returns only source IDs for display ordering.  The
canonical ``DiscoveryResult`` and its complete winning candidate set remain
unchanged, and no result from this module is an authority or semantic review
decision.
"""

from __future__ import annotations

import base64
import re
from collections import Counter
from typing import Final

from pre_pr_verify.discovery_models import DiscoveryResult, DiscoverySource
from pre_pr_verify.errors import PreflightError
from pre_pr_verify.models import ChangeSet


MAX_RELEVANCE_CANDIDATES: Final = 256
MAX_RECOMMENDED_REQUIREMENT_CANDIDATES: Final = 5
MAX_RELEVANCE_CHANGE_PATHS: Final = 64
MAX_RELEVANCE_CHANGED_CONTENT_CHARS: Final = 8_192
MAX_RELEVANCE_CANDIDATE_CONTENT_CHARS: Final = 2_048

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "are",
        "as",
        "be",
        "by",
        "can",
        "change",
        "changed",
        "changes",
        "code",
        "document",
        "documentation",
        "docs",
        "file",
        "files",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "must",
        "not",
        "of",
        "on",
        "only",
        "or",
        "repository",
        "requirement",
        "requirements",
        "should",
        "source",
        "spec",
        "specification",
        "test",
        "tests",
        "that",
        "the",
        "this",
        "to",
        "with",
        "will",
    }
)


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_PATTERN.findall(value)
        if len(token) >= 2 and token.casefold() not in _STOPWORDS
    }


def _add_signal_tokens(
    signals: Counter[str],
    value: str,
    weight: int,
) -> set[str]:
    tokens = _tokens(value)
    for token in tokens:
        signals[token] += weight
    return tokens


def _changed_signals(changeset: ChangeSet) -> tuple[Counter[str], set[str]]:
    signals: Counter[str] = Counter()
    changed_path_tokens: set[str] = set()
    content_by_digest = {blob.sha256: blob for blob in changeset.contents}
    remaining_content = MAX_RELEVANCE_CHANGED_CONTENT_CHARS

    for change in changeset.changes[:MAX_RELEVANCE_CHANGE_PATHS]:
        changed_path_tokens.update(
            _add_signal_tokens(signals, change.effective.path.display, 4)
        )
        state = change.effective
        if (
            remaining_content <= 0
            or not state.content_captured
            or state.binary is True
        ):
            continue
        blob = content_by_digest.get(state.content_identity)
        if blob is None:
            continue
        try:
            content = base64.b64decode(blob.data_b64, validate=True).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        excerpt = content[:remaining_content]
        _add_signal_tokens(signals, excerpt, 1)
        remaining_content -= len(excerpt)

    return signals, changed_path_tokens


def _source_score(
    source: DiscoverySource,
    signals: Counter[str],
) -> tuple[int, int, int]:
    path_tokens = _tokens(source.path.display if source.path is not None else "")
    label_tokens = _tokens(source.label)
    content_tokens = _tokens(source.content_utf8[:MAX_RELEVANCE_CANDIDATE_CONTENT_CHARS])

    def overlap_score(tokens: set[str]) -> int:
        return sum(signals[token] for token in tokens if token in signals)

    return (
        overlap_score(path_tokens),
        overlap_score(label_tokens),
        overlap_score(content_tokens),
    )


def recommend_requirement_source_ids(
    changeset: ChangeSet,
    discovery: DiscoveryResult,
) -> tuple[str, ...]:
    """Return a bounded, deterministic presentation recommendation.

    Only the complete winning requirement candidate IDs from ``discovery`` are
    considered.  A recommendation is empty for zero or one candidate, when no
    bounded lexical overlap exists, or when all candidates tie at zero.  Ties
    are resolved by the canonical candidate order already established by
    ``DiscoveryResult``.
    """

    candidate_ids = tuple(discovery.requirement_resolution.candidate_source_ids)
    if len(candidate_ids) <= 1:
        return ()
    if len(candidate_ids) > MAX_RELEVANCE_CANDIDATES:
        raise PreflightError("requirement relevance candidate set exceeds its bound")

    sources = {source.source_id: source for source in discovery.sources}
    if any(source_id not in sources for source_id in candidate_ids):
        raise PreflightError("requirement relevance references an unknown source")

    signals, _ = _changed_signals(changeset)
    scored: list[tuple[tuple[int, int, int], int, str]] = []
    for canonical_index, source_id in enumerate(candidate_ids):
        score = _source_score(sources[source_id], signals)
        if any(score):
            scored.append((score, canonical_index, source_id))

    scored.sort(
        key=lambda item: (
            -item[0][0],
            -item[0][1],
            -item[0][2],
            item[1],
            item[2],
        )
    )
    return tuple(
        source_id
        for _score, _canonical_index, source_id in scored[
            :MAX_RECOMMENDED_REQUIREMENT_CANDIDATES
        ]
    )
