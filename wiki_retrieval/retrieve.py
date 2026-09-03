# 统一检索入口 retrieve(query, scope, channels, top_k, rrf_k, include_content, verbose)；scope 限定 wiki 或 source；语料为 0 时抛错
"""The single retrieval entry point used by the MCP tool and by evaluation.

Failure semantics, in increasing severity:
  * a query that matches nothing  -> normal response with `results: []`
  * a channel that cannot answer  -> that channel is absent, the rest are intact
  * a corpus with no documents    -> CorpusEmptyError, because no query can work
"""

from __future__ import annotations

import time
from pathlib import Path

from . import dedup, fusion, index, loader
from .channels import graph, keyword, vector

DEFAULT_TOP_K = 10
MAX_RESULTS = 50
ALL_CHANNELS = ("vector", "keyword", "graph")
SCOPES = ("wiki", "source", "both")
SUMMARY_SIZE = 5
MAX_CHUNKS_PER_PAGE = 2
MAX_EVIDENCE_CHARS = 8000


def _prioritize_chunks(chunks: list[dict]) -> list[dict]:
    """Keep complementary exact-term and semantic evidence for each page."""
    groups = (
        sorted(
            (chunk for chunk in chunks if len(chunk.get("scores", {})) > 1),
            key=lambda chunk: (-len(chunk.get("scores", {})), chunk["chunk_index"]),
        ),
        sorted(
            (chunk for chunk in chunks if "keyword" in chunk.get("scores", {})),
            key=lambda chunk: (-chunk["scores"]["keyword"], chunk["chunk_index"]),
        ),
        sorted(
            (chunk for chunk in chunks if "vector" in chunk.get("scores", {})),
            key=lambda chunk: (-chunk["scores"]["vector"], chunk["chunk_index"]),
        ),
        chunks,
    )
    selected: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for chunk in group:
            if chunk["chunk_id"] in seen:
                continue
            selected.append(chunk)
            seen.add(chunk["chunk_id"])
            if len(selected) == MAX_CHUNKS_PER_PAGE:
                return selected
    return selected


def _bound_evidence(ranking: list[dict], include_content: bool) -> None:
    """Keep evidence compact without slicing through a semantic chunk."""
    remaining = MAX_EVIDENCE_CHARS
    for result_index, entry in enumerate(ranking):
        chunks = _prioritize_chunks(entry.get("matched_chunks", []))
        if not include_content:
            for chunk in chunks:
                chunk.pop("text", None)
            entry["matched_chunks"] = chunks
            continue

        bounded = []
        for chunk in chunks:
            text = chunk.get("text")
            if not isinstance(text, str):
                bounded.append(chunk)
                continue
            if len(text) <= remaining:
                bounded.append(chunk)
                remaining -= len(text)
            elif result_index == 0 and not bounded:
                # Chunk construction guarantees <= 2000 chars, so this is only
                # defensive if an older or externally-created index is queried.
                fallback = dict(chunk)
                fallback["text"] = text[:MAX_EVIDENCE_CHARS]
                bounded.append(fallback)
                remaining = 0
            else:
                metadata = dict(chunk)
                metadata.pop("text", None)
                bounded.append(metadata)
        entry["matched_chunks"] = bounded


def _normalize_channels(channels: list[str] | None, scope: str) -> list[str]:
    requested = list(ALL_CHANNELS) if channels is None else [str(c).strip().lower() for c in channels]
    unknown = [name for name in requested if name not in ALL_CHANNELS]
    if unknown:
        raise ValueError(
            f"unknown channel(s) {unknown}; expected any of {list(ALL_CHANNELS)}"
        )
    if not requested:
        raise ValueError("at least one channel is required")
    if scope == "source":
        # The graph is built from wikilinks, so it has nothing to say about sources.
        requested = [name for name in requested if name != "graph"]
        if not requested:
            raise ValueError("scope 'source' cannot be served by the graph channel alone")
    ordered = [name for name in ALL_CHANNELS if name in requested]
    return ordered


def retrieve(
    query: str,
    scope: str = "wiki",
    channels: list[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    rrf_k: float = fusion.DEFAULT_RRF_K,
    include_content: bool = False,
    verbose: bool = False,
    root: Path | None = None,
) -> dict:
    started = time.perf_counter()
    if not query or not query.strip():
        raise ValueError("query is required")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {list(SCOPES)}, got '{scope}'")
    active = _normalize_channels(channels, scope)
    limit = max(1, min(int(top_k), MAX_RESULTS))

    corpus = loader.get_corpus(loader.find_root() if root is None else root)
    if corpus.is_empty:
        raise loader.CorpusEmptyError(
            f"no documents under {corpus.root / loader.WIKI_DIR} or "
            f"{corpus.root / loader.SOURCE_DIR}"
        )

    previous = dedup.lookup(scope, active, query)

    runs: list[fusion.Run] = []
    embedding_status: dict = {"status": "disabled", "error": "vector channel not requested"}
    vector_hits = 0
    if "vector" in active:
        vector_run, embedding_status = vector.run(
            corpus, query, scope, limit, include_content
        )
        if vector_run is not None:
            vector_hits = len(vector_run.ordered)
            runs.append(vector_run)
    if "keyword" in active:
        runs.append(keyword.run(corpus, query, scope, include_content))
    if "graph" in active:
        runs.append(graph.run(corpus, query, include_content))

    fused = fusion.rrf(runs, rrf_k)
    ranking = fused["ranking"][:limit]
    for entry in ranking:
        entry["origin"] = "fused"

    graph_slots = 0
    graph_used = 0
    if "graph" in active:
        graph_slots = graph.quota(limit, vector_hits)
        graph_used = graph.expand(
            ranking, corpus, limit, graph_slots, include_content, fused["rrf_k"]
        )
        ranking.sort(key=lambda entry: (-entry["fused_score"], entry["path"]))

    _bound_evidence(ranking, include_content)

    payload = {
        "query": query,
        "scope": scope,
        "channels": active,
        "embedding": embedding_status,
        "duplicate": previous is not None,
        "corpus": corpus.totals(),
        "index": index.freshness(corpus.root),
        "results": ranking,
    }
    if previous is not None:
        payload["previous"] = previous
    if "graph" in active:
        payload["graph_expansion"] = {"slots": graph_slots, "used": graph_used}
    if verbose:
        payload["runs"] = fused["runs"]
        payload["timings"] = {
            "corpus_build_ms": corpus.built_ms,
            "channels_ms": {run.name: run.elapsed_ms for run in runs},
            "total_ms": (time.perf_counter() - started) * 1000.0,
        }

    dedup.remember(
        scope,
        active,
        query,
        {
            "result_count": len(ranking),
            "top_page_ids": [entry["page_id"] for entry in ranking[:SUMMARY_SIZE]],
        },
    )
    return payload
