"""Real-corpus retrieval evaluation; no generative model or semantic judge.

Run from the application root with ``python -m benchmarks.retrieval_eval.run_multikb``.
The answer metrics are deliberately conservative lexical/extractive proxies, not
validated answer-quality estimates. Proxy results never promote default features.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable

from benchmarks.retrieval_eval import multikb

APP_ROOT = Path(__file__).resolve().parents[2]
EVALUATOR_MODE = "deterministic_extractive_lexical_proxy_v1"


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff%.-]", "", text.casefold())


def terms(text: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9]+", text.casefold()))
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(segment[i:i + 2] for i in range(max(1, len(segment) - 1)))
    return result


def subqueries(query: str, limit: int) -> list[str]:
    """Original query plus surface conjunction clauses; never inspect gold facts."""
    candidates = [query]
    for part in re.split(r"[，；;？?]|以及|并且|与|和", query):
        part = part.strip()
        if len(part) >= 4 and part != query and part not in candidates:
            candidates.append(part)
    return candidates[:max(1, limit)]


def result_text(result: dict) -> str:
    chunks = [str(chunk.get("text", "")) for chunk in result.get("matched_chunks", [])]
    return "\n".join(text for text in chunks if text) or str(result.get("content") or result.get("text") or "")


def source_key(result: dict) -> str:
    return f"{result.get('kb_id', '')}:{result.get('path', result.get('source', ''))}"


def extract_answer(query: str, results: list[dict], max_chars: int) -> tuple[str, list[str]]:
    """Quote query-overlapping evidence sentences verbatim, without using gold labels."""
    selected, citations = [], []
    query_terms = terms(query)
    remaining = max_chars
    for result in results:
        sentences = re.split(r"(?<=[。！？])|\n", result_text(result))
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence or not query_terms.intersection(terms(sentence)):
                continue
            if len(sentence) > remaining:
                continue
            selected.append(sentence)
            remaining -= len(sentence)
            if source_key(result) not in citations:
                citations.append(source_key(result))
            if len(selected) >= 12:
                break
        if len(selected) >= 12 or remaining <= 0:
            break
    return ("\n".join(selected) if selected else "证据不足，未检索到可直接支持回答的内容。", citations)


def score_answer(question: dict, record: dict, answer: str, citations: list[str]) -> dict:
    evidence = normalized("\n".join(result_text(row) for row in record["results"]))
    answer_norm = normalized(answer)
    required = question["required_facts"]
    # Exact normalized matches favor precision over semantic recall. Rubric phrases
    # are NOT converted into answer text; missed paraphrases remain false negatives.
    matched = [fact for fact in required if normalized(fact) in answer_norm]
    forbidden = [fact for fact in question["forbidden_facts"] if normalized(fact) in answer_norm]
    expected = set(question["expected_sources"])
    cited = set(citations)
    has_abstention = not citations
    factual = len(matched) / max(1, len(required)) if not forbidden else 0.0
    conflict_cues = ("冲突", "差异", "异质性", "不能一概而论", "不一致", "不可直接", "范围")
    conflict = float(not question["conflict_expected"] or (
        len({value.split(":", 1)[0] for value in citations}) > 1
        and any(cue in answer for cue in conflict_cues)
    ))
    result = {key: record[key] for key in ("schema_version", "run_id", "question_id", "config_id", "repeat", "seed", "latency_ms", "tool_calls")}
    result.update(
        record_type="answer_score", factual_correctness=factual,
        citation_recall=len(expected & cited) / max(1, len(expected)),
        grounded_ratio=float(bool(citations) and all(normalized(line) in evidence for line in answer.splitlines() if line)),
        conflict_handling=conflict,
        abstention_score=float(has_abstention == (not question["answerable"])),
        hit_at_5=float(bool(expected & {source_key(row) for row in record["results"][:5]})),
        stale_fact_error=0, stale_fact_error_measured=False,
        answer=answer, citations=citations, evaluator_mode=EVALUATOR_MODE,
        matched_required_facts=matched, matched_forbidden_facts=forbidden,
        notes="Conservative exact-text proxy; stale_fact_error is an unmeasured schema placeholder, not a zero-error finding. No semantic or causal judgment.",
    )
    if record["status"] != "ok":
        for metric in multikb.SCORE_WEIGHTS:
            result[metric] = 0.0
    result["quality_score"] = multikb.quality_score(result)
    return result


def run_one(root: Path, question: dict, config: dict, spec: multikb.RunSpec,
            retrieve_fn: Callable[..., dict], allowed_channels: set[str] | None = None) -> tuple[dict, dict]:
    began, started = time.perf_counter(), stamp()
    options = deepcopy(config["retrieval"])
    budget = config["budget"]
    requested = [name for name in ("vector", "keyword", "graph") if options.get(name, name != "graph")]
    channels = [name for name in requested if allowed_channels is None or name in allowed_channels]
    behavior, degradations, failures, responses, merged = [], [], [], [], {}
    if channels != requested:
        degradations.append(f"channel filter: requested={requested}, active={channels}")
    if not channels:
        channels = ["keyword"]
        degradations.append("no requested channel available; explicit keyword fallback (not comparable to intended configuration)")
    scope = {"raw": "source", "all": "both", "wiki_first": "wiki"}.get(options.get("scope", "both"), options.get("scope", "both"))
    queries = subqueries(question["question"], int(budget["max_subqueries"])) if options.get("decompose") else [question["question"]]
    behavior.append({"decomposition": queries, "method": "surface_conjunction_split"})
    pending = [(query, scope) for query in queries]
    if options.get("raw_evidence_fallback") or options.get("scope") == "wiki_first":
        pending.append((question["question"], "source"))
    seen, calls = set(), 0
    stop = "completed"
    while pending:
        if calls >= int(budget["max_tool_calls"]):
            stop = "max_tool_calls"
            break
        if (time.perf_counter() - began) * 1000 >= int(budget["deadline_ms"]):
            stop = "deadline"
            break
        query, call_scope = pending.pop(0)
        if (query, call_scope) in seen:
            continue
        seen.add((query, call_scope))
        call_channels = [name for name in channels if call_scope != "source" or name != "graph"] or ["keyword"]
        call_options = {name: name in call_channels for name in ("vector", "keyword", "graph")}
        call_options["claim_freshness_mode"] = options.get("claim_freshness_mode", "off")
        calls += 1
        before = len(merged)
        try:
            response = retrieve_fn(query=query, root=root, scope=call_scope, channels=call_channels,
                include_content=True, top_k=10, overrides={"retrieval": call_options}, verbose=True)
            responses.append(response)
            for kb_id, error in response.get("errors", {}).items():
                failures.append(f"{kb_id}: {error}")
            for kb_id, status in response.get("kb_status", {}).items():
                embedding = status.get("embedding", {})
                if "vector" in call_channels and embedding.get("status") not in ("ok", "success", None):
                    degradations.append(f"{kb_id} embedding: {embedding.get('status')}: {embedding.get('error', '')}")
            for row in response.get("results", []):
                key = row.get("page_id") or source_key(row)
                if key not in merged:
                    merged[key] = row
                else:
                    # Keep additional evidence from a repeated page without duplicating chunks.
                    existing = {chunk.get("chunk_id") for chunk in merged[key].get("matched_chunks", [])}
                    merged[key].setdefault("matched_chunks", []).extend(
                        chunk for chunk in row.get("matched_chunks", []) if chunk.get("chunk_id") not in existing)
            behavior.append({"query": query, "scope": call_scope, "channels": call_channels, "new_pages": len(merged) - before})
            if options.get("sufficiency_check"):
                enough = len(merged) >= 3 and sum(len(result_text(row)) for row in merged.values()) >= 800
                behavior.append({"sufficiency_check": enough, "method": "at_least_3_pages_and_800_evidence_chars"})
                # Raw verification remains mandatory even after heuristic sufficiency.
                if enough and not any(s == "source" for _, s in pending):
                    stop = "heuristic_sufficient"
                    break
            if len(merged) == before and calls > 1 and not any(s == "source" for _, s in pending):
                stop = "no_new_pages"
                break
            if not pending and not merged and options.get("auto_supplement"):
                fallback = " ".join(sorted(terms(question["question"])))
                if (fallback, scope) not in seen:
                    pending.append((fallback, scope))
        except Exception as error:
            failures.append(f"{type(error).__name__}: {error}")
    results = list(merged.values())
    # Preserve first-pass ranking, then append newly found pages. Scores from
    # different subqueries are not treated as calibrated probabilities.
    record = dict(multikb.spec_as_dict(spec), schema_version=1, record_type="retrieval_run",
        status="retrieval_error" if failures else "ok", started_at=started, finished_at=stamp(),
        latency_ms=(time.perf_counter() - began) * 1000, tool_calls=calls, results=results,
        kb_ids=sorted({row.get("kb_id", "") for row in results}), result_count=len(results),
        top_sources=[source_key(row) for row in results[:10]],
        route_reason=json.dumps([response.get("routing", {}) for response in responses], ensure_ascii=False),
        failure_detail="; ".join(failures), degradations=degradations, behavior=behavior,
        stop_reason=stop, config_snapshot=config, evaluator_mode=EVALUATOR_MODE,
        budget_note="Deadline checked between synchronous tool calls; an in-flight embedding request may overrun.")
    answer, citations = extract_answer(question["question"], results, int(budget["max_evidence_chars"]))
    return record, score_answer(question, record, answer, citations)


def source_manifest(root: Path) -> list[dict]:
    manifest = []
    for path in sorted((root / "kb").glob("*/raw/sources/*.md")):
        content = path.read_text(encoding="utf-8")
        fields = {}
        if content.startswith("---\n"):
            for line in content.split("---", 2)[1].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip().strip('"\'')
        manifest.append({"kb_id": path.parents[2].name, "source_id": path.stem,
            "title": fields.get("title", path.stem), "publisher": fields.get("publisher", "unknown"),
            "published_at": fields.get("published_at", "unknown"), "effective_at": fields.get("effective_from", "unknown"),
            "accessed_at": fields.get("accessed_at", "unknown"), "license": fields.get("license", "unknown"),
            "sha256": fields.get("sha256", hashlib.sha256(path.read_bytes()).hexdigest()),
            "markdown_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_url": fields.get("source_url", ""), "local_path": path.relative_to(root).as_posix(),
            "status": fields.get("access_status", "local_existing"), "notes": fields.get("extraction_type", "")})
    return manifest


def run_evaluation(root: Path, output_dir: Path, *, retrieve_fn: Callable[..., dict] | None = None,
                   allowed_channels: set[str] | None = None) -> dict:
    if retrieve_fn is None:
        from wiki_retrieval.embedding import load_dotenv
        from wiki_retrieval.retrieve import retrieve
        load_dotenv(root)
        retrieve_fn = retrieve
    questions, configs = multikb.load_questions(), multikb.load_configs()
    by_question = {row["id"]: row for row in questions}
    by_config = {row["id"]: row for group in configs.values() for row in group}
    started = stamp()
    runs, scores = [], []
    output_dir.mkdir(parents=True, exist_ok=True)

    def save_progress(run, score):
        # Persist each completed run so interruption never loses the experiment.
        for name, value in (("retrieval_runs.jsonl", run), ("answer_scores.jsonl", score)):
            with (output_dir / name).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        print(f"completed {len(runs)}: {run['run_id']} ({run['status']})", flush=True)

    if any((output_dir / name).exists() for name in ("retrieval_runs.jsonl", "answer_scores.jsonl", "report.json")):
        raise FileExistsError("Use a new output directory to preserve previous experiment records")
    for phase, schedule in (("ablation", multikb.initial_ablation_schedule(questions)),
                            ("freshness", multikb.initial_freshness_schedule(questions))):
        phase_scores = []
        for spec in schedule:
            run, score = run_one(root, by_question[spec.question_id], by_config[spec.config_id], spec, retrieve_fn, allowed_channels)
            runs.append(run)
            scores.append(score)
            phase_scores.append(score)
            save_progress(run, score)
        for spec in multikb.staged_repeat_schedule(questions, phase_scores, phase=phase):
            run, score = run_one(root, by_question[spec.question_id], by_config[spec.config_id], spec, retrieve_fn, allowed_channels)
            runs.append(run)
            scores.append(score)
            save_progress(run, score)
    aggregates = multikb.aggregate_scores(scores)
    for row in aggregates:
        row["stale_fact_error_rate"] = None
        row.update(name=by_config[row["config_id"]]["name"], promote=False,
            decision_reason="Proxy evaluator only; semantic answer validation and measured stale-error labels required before promotion.")
    ablations = [row for row in aggregates if row["config_id"].startswith("C")]
    freshness = [dict(row, ordinary_quality_drop=None, stale_error_reduction=None) for row in aggregates if row["config_id"].startswith("F")]
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    report = {"run_summary": {"run_id": output_dir.name, "started_at": started, "finished_at": stamp(),
        "git_commit": commit, "app_root": str(root), "evaluator_mode": EVALUATOR_MODE,
        "model": "none (deterministic extractive proxy)", "allowed_channels": ",".join(sorted(allowed_channels)) if allowed_channels is not None else "all",
        "run_count": len(runs), "degraded_runs": sum(bool(row["degradations"]) for row in runs),
        "limitations": "Lexical rubric matching, no semantic judge; no calibrated stale-fact labels; no feature auto-promotion; timings include warm-cache repeats."},
        "source_manifest": source_manifest(root), "questions": questions, "retrieval_runs": runs,
        "answer_scores": scores, "freshness_ablation": freshness, "aggregate": ablations,
        "pareto": multikb.pareto_frontier(ablations),
        "failures": [dict(row, failure_detail="; ".join(filter(None, [row["failure_detail"], *row["degradations"]]))) for row in runs if row["failure_detail"] or row["degradations"]],
        "config_metadata": configs, "embedding_metadata": {}, "schema_version": 1}
    for path in (root / "kb").glob("*/.lancedb/index_meta.json"):
        report["embedding_metadata"][path.parents[1].name] = json.loads(path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    multikb.write_jsonl(output_dir / "retrieval_runs.jsonl", runs)
    multikb.write_jsonl(output_dir / "answer_scores.jsonl", scores)
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    parser.add_argument("--output-dir", type=Path, default=APP_ROOT / "benchmarks/retrieval_eval/artifacts" / datetime.now(timezone.utc).strftime("multikb-%Y%m%dT%H%M%SZ"))
    parser.add_argument("--channels", choices=("vector", "keyword", "graph"), nargs="+")
    parser.add_argument("--no-vector", action="store_true")
    args = parser.parse_args(argv)
    allowed = set(args.channels) if args.channels else None
    if args.no_vector:
        allowed = (allowed or {"keyword", "graph"}) - {"vector"}
    report = run_evaluation(args.root.resolve(), args.output_dir.resolve(), allowed_channels=allowed)
    print(json.dumps({"report": str(args.output_dir / "report.json"), **report["run_summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
