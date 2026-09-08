"""Federated retrieval: catalogue routing, isolated failures and rank fusion."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import time

from . import embedding
from .textutil import expand_conversational_query


def registry(root: Path) -> dict[str, dict]:
    root = Path(root).resolve()
    config = root / "mobilework.config.json"
    if not config.is_file():
        return {root.name: {"kb_id": root.name, "name": root.name, "root": root, "priority": 1.0}}
    entries = json.loads(config.read_text(encoding="utf-8"))["knowledge_bases"]
    result = {}
    for identifier, item in entries.items():
        if not item.get("enabled", True):
            continue
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("knowledge base path must stay inside project")
        priority = float(item.get("priority", 1))
        if not math.isfinite(priority) or priority <= 0:
            raise ValueError("KB priority must be finite and positive")
        result[identifier] = {"kb_id": identifier, "name": item.get("name", identifier), "root": path, "priority": priority}
    return result


def list_knowledge_bases(root: Path) -> dict:
    return {"knowledge_bases": [{**item, "root": str(item["root"])} for item in registry(root).values()]}


def _tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.update(segment[i:i + 2] for i in range(max(1, len(segment) - 1)))
    return tokens


def route_knowledge_bases(root: Path, query: str, kb_ids: list[str] | None = None, top_k: int = 3) -> dict:
    if not query.strip():
        raise ValueError("query is required")
    entries = registry(root)
    candidates = list(entries) if kb_ids is None else list(dict.fromkeys(kb_ids))
    if not candidates or set(candidates) - entries.keys():
        raise ValueError("kb_ids must contain enabled knowledge base IDs")
    routing_query, expansion_terms = expand_conversational_query(query)
    terms = _tokens(routing_query)
    canonical_terms = _tokens(" ".join(expansion_terms))
    scored = []
    for identifier in candidates:
        path = entries[identifier]["root"] / "wiki/index.md"
        catalog = path.read_text(encoding="utf-8") if path.is_file() else ""
        catalog_terms = _tokens(catalog)
        lexical_score = len(terms & catalog_terms) / max(1, len(terms))
        canonical_score = (
            len(canonical_terms & catalog_terms) / len(canonical_terms)
            if canonical_terms else 0.0
        )
        score = max(lexical_score, canonical_score)
        scored.append({"kb_id": identifier, "score": score})
    scored.sort(key=lambda item: (-item["score"], item["kb_id"]))
    confident = scored[0]["score"] >= .5
    selected = [item["kb_id"] for item in scored if item["score"] >= .5][:max(1, top_k)] if confident else candidates
    return {"selected_kb_ids": selected, "candidate_kb_ids": candidates, "scores": scored,
            "fallback": not confident, "query_expansion": {"applied": bool(expansion_terms),
            "terms": expansion_terms}}


def retrieve(root: Path, query: str, kb_ids: list[str] | None = None,
             channel_weights: dict | None = None, claim_freshness_mode: str = "off", **kwargs) -> dict:
    from .retrieve import retrieve as local_retrieve, _bound_evidence
    from .claims import enrich
    started = time.perf_counter()
    from .retrieve import _normalize_channels, SCOPES
    scope = kwargs.get("scope", "wiki")
    if scope not in SCOPES:
        raise ValueError("invalid scope")
    _normalize_channels(kwargs.get("channels"), scope)
    if claim_freshness_mode not in ("off", "context", "rerank", "both"):
        raise ValueError("invalid claim_freshness_mode")
    weights = channel_weights or {}
    if any(not math.isfinite(float(v)) or float(v) < 0 for v in weights.values()):
        raise ValueError("channel weights must be finite and non-negative")
    entries = registry(root)
    route = route_knowledge_bases(root, query, kb_ids)
    limit = max(1, min(50, int(kwargs.get("top_k", 10))))
    rrf_k = float(kwargs.get("rrf_k", 60))
    if not 1 <= rrf_k <= 10000:
        raise ValueError("rrf_k must be between 1 and 10000")
    responses, errors = {}, {}

    def execute(ids):
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(ids)))) as pool:
            futures = {pool.submit(local_retrieve, query, root=entries[i]["root"], **kwargs): i for i in ids}
            for future in as_completed(futures):
                identifier = futures[future]
                try:
                    responses[identifier] = future.result()
                except Exception as error:
                    errors[identifier] = embedding.redact(str(error))

    execute(route["selected_kb_ids"])
    if not any(item["results"] for item in responses.values()):
        remaining = [i for i in route["candidate_kb_ids"] if i not in responses and i not in errors]
        if remaining:
            execute(remaining)
            route["fallback"] = True
    ranking = []
    for identifier in sorted(responses):
        item = entries[identifier]
        for rank, original in enumerate(responses[identifier]["results"], 1):
            entry = deepcopy(original)
            entry.update(kb_id=identifier, kb_name=item["name"], local_page_id=entry["page_id"], rank_in_kb=rank)
            entry["page_id"] = f"{identifier}::{entry['page_id']}"
            entry["global_score"] = item["priority"] * sum(float(weights.get(channel, 1)) for channel in entry["ranks"]) / (rrf_k + rank)
            entry["fused_score"] = entry["global_score"]
            enrich(entry, item["root"], query, claim_freshness_mode)
            for chunk in entry.get("matched_chunks", []):
                chunk["local_chunk_id"] = chunk["chunk_id"]
                chunk["chunk_id"] = f"{identifier}::{chunk['chunk_id']}"
                chunk["page_id"] = entry["page_id"]
            ranking.append(entry)
    ranking.sort(key=lambda e: (-e["fused_score"], e["kb_id"], e["path"]))
    ranking = ranking[:limit]
    _bound_evidence(ranking, kwargs.get("include_content", False))
    return {"query": query, "scope": kwargs.get("scope", "wiki"), "results": ranking,
            "routing": route, "queried_kb_ids": sorted(set(responses) | set(errors)),
            "errors": errors, "partial_failure": bool(errors), "duplicate": bool(responses) and all(r.get("duplicate") for r in responses.values()),
            "kb_status": {i: {k: v for k, v in r.items() if k != "results"} for i, r in responses.items()},
            "timings": {"total_ms": (time.perf_counter() - started) * 1000}}
