# 图通道：wikilink 无向邻接、按 query 召回、graph_neighbors 种子扩展
# ---------------------------------------------------------------------------
# GPLv3 provenance — see third_party/wiki-retrieval-mcp/UPSTREAM.md
# Derived from github.com/chengtyao2-design/llm_wiki @ a7b542e (GPLv3):
#   src-tauri/src/commands/search.rs, .../vectorstore.rs
# This module is excluded from git until replaced by a self-owned implementation.
# ---------------------------------------------------------------------------
"""Graph channel over the wikilink adjacency.

Three distinct jobs share one adjacency: `run` recalls entities for a query and
takes part in RRF, `expand` reserves a quota of the final slots for one-hop
neighbours of the top hits, and `neighbors` serves the graph_neighbors tool.
"""

from __future__ import annotations

import math
import time

from ..fusion import Run
from ..loader import Corpus, Doc, result_stub
from ..textutil import alias, split_terms

NAME = "graph"

MATCHED_ENTITY_SCORE = 10_000.0
NEIGHBOUR_SCORE = 5_000.0
MIN_GRAPH_RESULT_RATIO = 0.15
MAX_GRAPH_RESULT_RATIO = 0.30
MAX_GRAPH_SEEDS = 20
DEGREE_TIEBREAK = 0.001


# provenance: upstream search.rs:495 (GPLv3)
def build_adjacency(docs: list[Doc]) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Undirected adjacency over resolvable wikilinks. A page is reachable by its
    path, its path without the wiki/ prefix, its page id and its title."""
    aliases: dict[str, str] = {}
    for doc in docs:
        without_prefix = (
            doc.path[len("wiki/") :] if doc.path.startswith("wiki/") else doc.path
        )
        for candidate in (doc.path, without_prefix, doc.page_id, doc.title):
            aliases[alias(candidate)] = doc.path
    adjacency: dict[str, set[str]] = {}
    for doc in docs:
        for link in doc.links:
            target = aliases.get(alias(link))
            if target is None or target == doc.path:
                continue
            adjacency.setdefault(doc.path, set()).add(target)
            adjacency.setdefault(target, set()).add(doc.path)
    return adjacency, aliases


def _backlinks(corpus: Corpus) -> dict[str, set[str]]:
    """Directed, unlike the adjacency: only the linking page counts as a backlink."""
    out: dict[str, set[str]] = {}
    for doc in corpus.wiki_docs:
        for link in doc.links:
            target = corpus.aliases.get(alias(link))
            if target is None or target == doc.path:
                continue
            out.setdefault(target, set()).add(doc.path)
    return out


def _matches(haystack: str, phrase: str, terms: list[str]) -> bool:
    return phrase in haystack or any(term in haystack for term in terms)


# provenance: upstream search.rs:253 (tools graph search) (GPLv3)
def run(corpus: Corpus, query: str, include_content: bool = False) -> Run:
    """Matched entities outrank their direct neighbours by a wide margin, so the
    ordering is stable regardless of degree."""
    started = time.perf_counter()
    phrase = query.strip().lower()
    if not phrase:
        return Run(name=NAME)

    terms = split_terms(phrase)
    docs = {doc.path: doc for doc in corpus.wiki_docs}
    seeds = {
        path
        for path, doc in docs.items()
        if _matches(f"{doc.title} {doc.path} {doc.content}".lower(), phrase, terms)
    }
    backlinks = _backlinks(corpus)

    scores: dict[str, float] = {}
    entries: dict[str, dict] = {}
    for path, doc in docs.items():
        neighbours = corpus.adjacency.get(path, set())
        matched = path in seeds
        connected = any(neighbour in seeds for neighbour in neighbours)
        if not matched and not connected:
            continue
        degree = len(neighbours) + len(backlinks.get(path, set()))
        scores[path] = (MATCHED_ENTITY_SCORE if matched else NEIGHBOUR_SCORE) + degree
        related = sorted(docs[n].title for n in neighbours if n in seeds)
        entry = result_stub(doc, anchor=phrase)
        entry["snippet"] = (
            f"{'matched entity' if matched else 'direct neighbor'}; "
            f"{len(neighbours)} related link(s)"
        )
        entry["graph_related_to"] = related
        entries[path] = entry

    ordered = sorted(scores, key=lambda path: (-scores[path], path))
    return Run(
        name=NAME,
        ordered=ordered,
        scores=scores,
        entries=entries,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


# provenance: upstream search.rs:522 (GPLv3)
def quota(limit: int, vector_hits: int) -> int:
    """Reserved graph slots shrink as the vector channel covers more of the page."""
    if limit < 2:
        return 0
    coverage = min(vector_hits, limit) / limit
    ratio = MAX_GRAPH_RESULT_RATIO - (
        MAX_GRAPH_RESULT_RATIO - MIN_GRAPH_RESULT_RATIO
    ) * coverage
    return max(1, min(math.ceil(limit * ratio), limit - 1))


# provenance: upstream search.rs:539 (GPLv3)
def expand(
    ranking: list[dict],
    corpus: Corpus,
    limit: int,
    slots: int,
    include_content: bool,
    rrf_k: float,
) -> int:
    """Replaces the tail of `ranking` in place with up to `slots` one-hop
    neighbours of the top hits. Returns how many slots were actually used."""
    if not ranking or slots <= 0:
        del ranking[limit:]
        return 0
    seeds = [entry["path"] for entry in ranking[: min(limit, MAX_GRAPH_SEEDS)]]
    seed_set = set(seeds)
    docs = {doc.path: doc for doc in corpus.wiki_docs}

    scores: dict[str, float] = {}
    related: dict[str, set[str]] = {}
    for rank, seed in enumerate(seeds):
        for neighbour in corpus.adjacency.get(seed, ()):
            if neighbour in seed_set:
                continue
            scores[neighbour] = scores.get(neighbour, 0.0) + 1.0 / (rank + 1)
            seed_doc = docs.get(seed)
            if seed_doc is not None:
                related.setdefault(neighbour, set()).add(seed_doc.title)

    candidates = [
        (path, score)
        for path, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if path in docs
    ][:slots]
    if not candidates:
        del ranking[limit:]
        return 0

    selected = {path for path, _ in candidates}
    kept = [entry for entry in ranking if entry["path"] not in selected][
        : limit - len(candidates)
    ]
    for path, score in candidates:
        titles = sorted(related.get(path, set()))
        supplement = result_stub(docs[path])
        supplement["fused_score"] = score / (rrf_k + 1.0)
        supplement["snippet"] = f"Graph neighbor of {', '.join(titles)}"
        supplement["graph_related_to"] = titles
        supplement["origin"] = "graph_expand"
        kept.append(supplement)
    ranking[:] = kept
    return len(candidates)


def neighbors(corpus: Corpus, seed: str, depth: int = 1, top_k: int = 10) -> dict:
    """Breadth-first walk out to `depth`. `seed` may be a page id, a path or a
    title; an unresolvable seed is a caller error, not an empty result."""
    if not seed or not seed.strip():
        raise ValueError("seed is required")
    depth = max(1, int(depth))
    top_k = max(1, int(top_k))

    resolved = corpus.aliases.get(alias(seed.strip()))
    if resolved is None:
        raise ValueError(f"seed '{seed}' does not resolve to a wiki page")

    docs = {doc.path: doc for doc in corpus.wiki_docs}
    distances: dict[str, int] = {resolved: 0}
    frontier = [resolved]
    for step in range(1, depth + 1):
        nxt: list[str] = []
        for path in frontier:
            for neighbour in sorted(corpus.adjacency.get(path, ())):
                if neighbour in distances:
                    continue
                distances[neighbour] = step
                nxt.append(neighbour)
        frontier = nxt
        if not frontier:
            break

    found = []
    for path, distance in distances.items():
        if distance == 0 or path not in docs:
            continue
        degree = len(corpus.adjacency.get(path, ()))
        found.append(
            {
                "page_id": docs[path].page_id,
                "path": path,
                "title": docs[path].title,
                "distance": distance,
                "score": 1.0 / distance + degree * DEGREE_TIEBREAK,
            }
        )
    found.sort(key=lambda item: (-item["score"], item["path"]))
    return {
        "seed": seed,
        "resolved_path": resolved,
        "depth": depth,
        "neighbors": found[:top_k],
    }
