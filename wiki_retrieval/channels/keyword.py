# 关键词通道：词项与短语打分（先沿用移植版策略，后续替换为自有实现）
# ---------------------------------------------------------------------------
# GPLv3 provenance — see third_party/wiki-retrieval-mcp/UPSTREAM.md
# Derived from github.com/chengtyao2-design/llm_wiki @ a7b542e (GPLv3):
#   src-tauri/src/commands/search.rs, .../vectorstore.rs
# This module is excluded from git until replaced by a self-owned implementation.
# ---------------------------------------------------------------------------
"""Keyword channel: phrase and token scoring over the loaded corpus.

The weights below are load-bearing for parity with the upstream runtime; the
whole file is scheduled for replacement by a self-owned scoring implementation.
"""

from __future__ import annotations

import time

from ..fusion import Run
from ..loader import Corpus, Doc, chunk_result, result_stub
from ..textutil import snippet, tokenize_query, trim_punctuation

NAME = "keyword"

FILENAME_EXACT_BONUS = 200.0
PHRASE_IN_TITLE_BONUS = 50.0
PHRASE_IN_CONTENT_PER_OCC = 20.0
CONTENT_TOKEN_WEIGHT = 1.0
PHRASE_OCCURRENCE_CAP = 10


# provenance: upstream search.rs:427 (GPLv3)
def _title_score(doc: Doc, query: str, phrase: str) -> float:
    score = 0.0
    normalized_query = trim_punctuation(query.lower())
    if doc.page_id.lower() == normalized_query:
        score += FILENAME_EXACT_BONUS
    if phrase and phrase in doc.lower_title_text:
        score += PHRASE_IN_TITLE_BONUS
    return score


def score_text(
    content: str,
    tokens: list[str],
    phrase: str,
) -> tuple[float, str] | None:
    """Returns (score, snippet anchor) or None when nothing in the doc matched."""
    content = content.lower()

    score = 0.0
    matched = False
    if phrase:
        occurrences = min(content.count(phrase), PHRASE_OCCURRENCE_CAP)
        if occurrences:
            score += occurrences * PHRASE_IN_CONTENT_PER_OCC
            matched = True

    for token in tokens:
        if token in content:
            matched = True
            score += CONTENT_TOKEN_WEIGHT

    if not matched:
        return None

    if phrase and phrase in content:
        anchor = phrase
    else:
        anchor = next((token for token in tokens if token in content), phrase)
    return score, anchor


# provenance: upstream search.rs:227 (keyword ranking half) (GPLv3)
def run(corpus: Corpus, query: str, scope: str, include_content: bool = False) -> Run:
    started = time.perf_counter()
    lowered = query.lower()
    tokens = tokenize_query(query) or [lowered.strip()]
    phrase = trim_punctuation(lowered)

    scores: dict[str, float] = {}
    entries: dict[str, dict] = {}
    for doc in corpus.docs(scope):
        page_score = _title_score(doc, query, phrase)
        chunk_scores: list[tuple[float, str, dict]] = []
        for chunk in doc.chunks:
            scored = score_text(chunk["text"], tokens, phrase)
            if scored is not None:
                score, anchor = scored
                chunk_scores.append((score, anchor, chunk))
        if not chunk_scores and page_score <= 0:
            continue
        if not chunk_scores:
            chunk_scores.append((0.0, query, doc.chunks[0]))
        chunk_scores.sort(key=lambda item: (-item[0], item[2]["chunk_index"]))
        score, anchor, best_chunk = chunk_scores[0]
        scores[doc.path] = page_score + score
        entry = result_stub(doc)
        entry["snippet"] = snippet(best_chunk["text"], anchor)
        entry["matched_chunks"] = [
            chunk_result(chunk, include_content, keyword=chunk_score)
            for chunk_score, _, chunk in chunk_scores[:2]
        ]
        entries[doc.path] = entry

    ordered = sorted(scores, key=lambda path: (-scores[path], path))
    return Run(
        name=NAME,
        ordered=ordered,
        scores=scores,
        entries=entries,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )
