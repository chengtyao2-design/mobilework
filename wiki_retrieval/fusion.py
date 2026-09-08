# RRF 融合：保留每路原始 rank 与融合分，供评测组做通道消融
"""Reciprocal rank fusion over independent channel runs.

Every run keeps its untruncated ordering and its raw per-document score, and the
fused ranking records each channel's rank and contribution separately. That is
what lets the eval team ablate a channel from a single stored response instead of
re-running retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_RRF_K = 60.0
MIN_RRF_K = 1.0
MAX_RRF_K = 10_000.0


@dataclass(slots=True)
class Run:
    """One channel's output. `ordered` holds document paths, best first."""

    name: str
    ordered: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    entries: dict[str, dict] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def ranks(self) -> dict[str, int]:
        return {path: index + 1 for index, path in enumerate(self.ordered)}


def _merge(target: dict, incoming: dict) -> None:
    """First run to see a document owns its identity fields; later runs only fill
    gaps and union the chunk list, so a vector-only hit still gets a snippet."""
    for key, value in incoming.items():
        if key in ("ranks", "raw_scores", "contributions", "fused_score"):
            continue
        if key == "matched_chunks":
            known = {chunk["chunk_id"]: chunk for chunk in target["matched_chunks"]}
            for chunk in value:
                existing = known.get(chunk["chunk_id"])
                if existing is None:
                    target["matched_chunks"].append(chunk)
                    known[chunk["chunk_id"]] = chunk
                    continue
                existing.setdefault("scores", {}).update(chunk.get("scores", {}))
                if "text" not in existing and "text" in chunk:
                    existing["text"] = chunk["text"]
            continue
        if key == "graph_related_to":
            target[key] = target[key] or value
            continue
        if not target.get(key):
            target[key] = value


def rrf(
    runs: list[Run],
    rrf_k: float = DEFAULT_RRF_K,
    top_k: int | None = None,
    weights: dict[str, float] | None = None,
) -> dict:
    if not MIN_RRF_K <= float(rrf_k) <= MAX_RRF_K:
        raise ValueError(f"rrf_k must be between {MIN_RRF_K:.0f} and {MAX_RRF_K:.0f}")
    rrf_k = float(rrf_k)
    channel_weights = weights or {}
    if any(float(weight) < 0 for weight in channel_weights.values()):
        raise ValueError("RRF weights must be non-negative")

    merged: dict[str, dict] = {}
    for run in runs:
        ranks = run.ranks()
        for path, rank in ranks.items():
            entry = merged.get(path)
            if entry is None:
                entry = dict(run.entries[path])
                entry.setdefault("matched_chunks", [])
                entry.setdefault("graph_related_to", [])
                entry["ranks"] = {}
                entry["raw_scores"] = {}
                entry["contributions"] = {}
                entry["fused_score"] = 0.0
                merged[path] = entry
            else:
                _merge(entry, run.entries[path])
            contribution = float(channel_weights.get(run.name, 1.0)) / (rrf_k + rank)
            entry["ranks"][run.name] = rank
            entry["raw_scores"][run.name] = run.scores.get(path, 0.0)
            entry["contributions"][run.name] = contribution
            entry["fused_score"] += contribution

    ranking = sorted(merged.values(), key=lambda entry: (-entry["fused_score"], entry["path"]))
    if top_k is not None:
        ranking = ranking[:top_k]
    return {
        "rrf_k": rrf_k,
        "runs": {
            run.name: {"ordered": list(run.ordered), "elapsed_ms": run.elapsed_ms}
            for run in runs
        },
        "ranking": ranking,
    }
