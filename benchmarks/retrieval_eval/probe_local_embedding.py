"""Probe local embedding variants against the agreed 12-question Wiki set."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from pathlib import Path

from benchmarks.retrieval_eval import multikb
from benchmarks.retrieval_eval.system_comparison import _path_matches
from wiki_retrieval import dedup, index, loader, local_embedding
from wiki_retrieval.retrieve import retrieve

QUESTION_IDS = ("Q01", "Q03", "Q04", "Q06", "Q07", "Q08", "Q10", "Q12", "Q15", "Q18", "Q19", "Q20")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def run_variant(root: Path, variant: str, repeats: int) -> dict:
    os.environ["MOBILEWORK_ROOT"] = str(root)
    os.environ["EMBEDDING_PROVIDER"] = "local"
    os.environ["EMBEDDING_LOCAL_VARIANT"] = variant
    local_embedding.clear_runtime_cache()
    knowledge_bases = [root / "kb/kb_enterprise", root / "kb/kb_research"]
    started = time.perf_counter()
    metas = [index.build(kb, batch=16, progress=lambda _message: None) for kb in knowledge_bases]
    index_ms = (time.perf_counter() - started) * 1000
    questions = {item["id"]: item for item in multikb.load_questions()}
    rows, latencies, embedding_latencies = [], [], []
    for repeat in range(1, repeats + 1):
        for question_id in QUESTION_IDS:
            question = questions[question_id]
            dedup.reset()
            response = retrieve(
                question["question"], root=root, scope="wiki", top_k=5,
                include_content=True, verbose=True, channels=["vector"],
                kb_ids=["kb_enterprise", "kb_research"],
            )
            results = response["results"][:5]
            ranks = [position for position, result in enumerate(results, 1)
                     if any(_path_matches(expected, f"{result.get('kb_id', '')}:{result['path']}")
                            for expected in question["expected_sources"])]
            kb_status = response.get("kb_status", {})
            embed_ms = sum(float(status.get("embedding", {}).get("elapsed_ms", 0.0))
                           for status in kb_status.values())
            latency = float(response.get("timings", {}).get("total_ms", 0.0))
            latencies.append(latency)
            embedding_latencies.append(embed_ms)
            rows.append({
                "question_id": question_id, "repeat": repeat,
                "hit_at_5": int(bool(ranks)), "reciprocal_rank": 1 / min(ranks) if ranks else 0.0,
                "latency_ms": latency, "embedding_ms": embed_ms,
                "top5": [f"{item.get('kb_id', '')}:{item['path']}" for item in results],
            })
    first = [row for row in rows if row["repeat"] == 1]
    return {
        "variant": variant,
        "model": local_embedding.runtime_name(),
        "preprocess": local_embedding.preprocess_id(),
        "dimension": metas[0]["dim"],
        "index_rows": sum(meta["rows"] for meta in metas),
        "index_ms": index_ms,
        "model_bytes": (local_embedding.model_dir() / variant).stat().st_size,
        "hit_at_5": statistics.mean(row["hit_at_5"] for row in first),
        "mrr": statistics.mean(row["reciprocal_rank"] for row in first),
        "query_p50_ms": percentile(embedding_latencies, .5),
        "query_p95_ms": percentile(embedding_latencies, .95),
        "retrieval_p50_ms": percentile(latencies, .5),
        "retrieval_p95_ms": percentile(latencies, .95),
        "runs": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--variant", action="append", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    reports = [run_variant(root, variant, args.repeats) for variant in args.variant]
    payload = {"schema_version": 1, "questions": list(QUESTION_IDS), "reports": reports}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
