# 向量通道：query embedding → LanceDB 近邻
"""Vector channel.

`store.search` and `embedding.fetch` are called through their modules on purpose:
tests patch them there, and it keeps this file free of network code. A missing
key or a failed embedding call degrades this one channel and nothing else.
"""

from __future__ import annotations

import logging
import time

from .. import embedding, store
from ..fusion import Run
from ..loader import Corpus, chunk_result, result_stub
from ..textutil import snippet

NAME = "vector"
OVERFETCH = 3
MIN_OVERFETCH_BASE = 10

logger = logging.getLogger(__name__)


def run(
    corpus: Corpus,
    query: str,
    scope: str,
    top_k: int,
    include_content: bool = False,
) -> tuple[Run | None, dict]:
    """Returns (run, embedding status). The run is None whenever the channel could
    not produce anything, which keeps the other channels' output untouched."""
    status: dict = {"status": "disabled", "model": embedding.model_name(), "elapsed_ms": 0.0}
    if not embedding.has_api_key():
        status["error"] = "EMBEDDING_API_KEY is not set; vector channel skipped"
        return None, status

    started = time.perf_counter()
    try:
        query_vector, embed_ms = embedding.fetch(query)
    except Exception as error:  # a dead endpoint must not fail the whole query
        status["status"] = "degraded"
        status["error"] = embedding.redact(str(error))
        logger.warning("embedding degraded: %s", status["error"])
        return None, status

    status["status"] = "ok"
    status["elapsed_ms"] = embed_ms

    hits = store.search(
        corpus.root, query_vector, max(top_k, MIN_OVERFETCH_BASE) * OVERFETCH, scope=scope
    )
    docs = {doc.page_id: doc for doc in corpus.docs(scope)}

    scores: dict[str, float] = {}
    entries: dict[str, dict] = {}
    ordered: list[str] = []
    for hit in hits:
        doc = docs.get(hit["page_id"])
        if doc is None:
            continue
        indexed_chunk = next(
            (
                candidate
                for candidate in doc.chunks
                if candidate["chunk_id"] == hit["chunk_id"]
                or candidate["chunk_index"] == hit["chunk_index"]
            ),
            None,
        )
        chunk_text = hit.get("chunk_text") or (
            indexed_chunk["text"] if indexed_chunk is not None else ""
        )
        chunk = {
            "chunk_id": hit["chunk_id"],
            "page_id": hit["page_id"],
            "chunk_index": hit["chunk_index"],
            "heading_path": hit["heading_path"],
            "text": chunk_text,
            "claim_ids": indexed_chunk.get("claim_ids", []) if indexed_chunk else [],
        }
        if doc.path not in entries:
            entry = result_stub(doc)
            entry["snippet"] = snippet(chunk["text"], query)
            entries[doc.path] = entry
            scores[doc.path] = hit["score"]
            ordered.append(doc.path)
        entry = entries[doc.path]
        if len(entry["matched_chunks"]) < 2:
            entry["matched_chunks"].append(
                chunk_result(chunk, include_content, vector=hit["score"])
            )

    return (
        Run(
            name=NAME,
            ordered=ordered,
            scores=scores,
            entries=entries,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        ),
        status,
    )
