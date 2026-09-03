from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
DEFAULT_QUESTIONS = HERE / "questions.multikb.v1.json"
DEFAULT_CONFIGS = HERE / "configs.multikb.v1.json"
SCHEMA_PATH = HERE / "schemas" / "multikb-eval-record.v1.schema.json"

QUESTION_FIELDS = {
    "id",
    "question",
    "category",
    "target_kbs",
    "expected_sources",
    "required_facts",
    "forbidden_facts",
    "temporal_intent",
    "conflict_expected",
    "answerable",
}
ABLATION_IDS = tuple(f"C{number:02d}" for number in range(1, 10))
FRESHNESS_IDS = ("F0", "F1", "F2", "F3")
SCORE_WEIGHTS = {
    "factual_correctness": 0.35,
    "citation_recall": 0.25,
    "grounded_ratio": 0.20,
    "conflict_handling": 0.10,
    "abstention_score": 0.10,
}
THRESHOLD_EPSILON = 1e-12


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    question_id: str
    config_id: str
    repeat: int
    seed: int
    phase: str


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def load_questions(path: Path = DEFAULT_QUESTIONS) -> list[dict[str, Any]]:
    payload = _load_object(path)
    if payload.get("schema_version") != 1:
        raise ValueError("multi-KB questions must use schema_version 1")
    questions = payload.get("questions")
    if not isinstance(questions, list) or len(questions) != 20:
        raise ValueError("multi-KB question manifest must contain exactly 20 questions")
    seen: set[str] = set()
    for item in questions:
        if not isinstance(item, dict) or set(item) != QUESTION_FIELDS:
            raise ValueError(f"each question must contain exactly: {sorted(QUESTION_FIELDS)}")
        question_id = item["id"]
        if not isinstance(question_id, str) or question_id in seen:
            raise ValueError(f"invalid or duplicate question id: {question_id!r}")
        seen.add(question_id)
        for field in ("target_kbs", "expected_sources", "required_facts", "forbidden_facts"):
            if not isinstance(item[field], list) or not all(isinstance(v, str) for v in item[field]):
                raise ValueError(f"{question_id}.{field} must be a list of strings")
        if not item["target_kbs"] or not item["expected_sources"] or not item["required_facts"]:
            raise ValueError(f"{question_id} must define targets, expected sources and required facts")
        for field in ("temporal_intent", "conflict_expected", "answerable"):
            if not isinstance(item[field], bool):
                raise ValueError(f"{question_id}.{field} must be boolean")
    freshness = [q for q in questions if q["temporal_intent"] or q["conflict_expected"]]
    if len(freshness) != 8:
        raise ValueError("freshness ablation set must contain exactly 8 temporal/conflict questions")
    return questions


def load_configs(path: Path = DEFAULT_CONFIGS) -> dict[str, list[dict[str, Any]]]:
    payload = _load_object(path)
    if payload.get("schema_version") != 1:
        raise ValueError("multi-KB configs must use schema_version 1")
    ablations = payload.get("ablations")
    freshness = payload.get("freshness")
    if not isinstance(ablations, list) or tuple(c.get("id") for c in ablations) != ABLATION_IDS:
        raise ValueError(f"ablations must be ordered {ABLATION_IDS}")
    if not isinstance(freshness, list) or tuple(c.get("id") for c in freshness) != FRESHNESS_IDS:
        raise ValueError(f"freshness configs must be ordered {FRESHNESS_IDS}")
    required = {"id", "name", "profile", "retrieval", "budget"}
    budget_fields = {
        "deadline_ms",
        "max_tool_calls",
        "max_subqueries",
        "max_graph_depth",
        "max_evidence_chars",
    }
    for config in [*ablations, *freshness]:
        if not isinstance(config, dict) or required - set(config):
            raise ValueError("each config must include id, name, profile, retrieval and budget")
        if not isinstance(config["retrieval"], dict) or not isinstance(config["budget"], dict):
            raise ValueError(f"{config.get('id')}: retrieval and budget must be objects")
        if set(config["budget"]) != budget_fields:
            raise ValueError(f"{config.get('id')}: budget must contain exactly {sorted(budget_fields)}")
        if not all(isinstance(value, int) and value >= 0 for value in config["budget"].values()):
            raise ValueError(f"{config.get('id')}: budget values must be non-negative integers")
    return {"ablations": ablations, "freshness": freshness}


def freshness_question_ids(questions: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(question["id"])
        for question in questions
        if question["temporal_intent"] or question["conflict_expected"]
    ]


def make_schedule(
    question_ids: Sequence[str],
    config_ids: Sequence[str],
    *,
    repeats: Iterable[int],
    phase: str,
    base_seed: int = 20260904,
) -> list[RunSpec]:
    specs: list[RunSpec] = []
    for repeat in repeats:
        if repeat < 1:
            raise ValueError("repeat numbers must be positive")
        for config_offset, config_id in enumerate(config_ids):
            for question_offset, question_id in enumerate(question_ids):
                seed = base_seed + repeat * 100_000 + config_offset * 1_000 + question_offset
                run_id = f"{phase}-{config_id}-{question_id}-r{repeat}"
                specs.append(RunSpec(run_id, question_id, config_id, repeat, seed, phase))
    return specs


def initial_ablation_schedule(questions: Sequence[Mapping[str, Any]]) -> list[RunSpec]:
    return make_schedule(
        [str(q["id"]) for q in questions],
        ABLATION_IDS,
        repeats=(1,),
        phase="ablation",
    )


def initial_freshness_schedule(questions: Sequence[Mapping[str, Any]]) -> list[RunSpec]:
    return make_schedule(
        freshness_question_ids(questions),
        FRESHNESS_IDS,
        repeats=(1,),
        phase="freshness",
    )


def _p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def quality_score(record: Mapping[str, Any]) -> float:
    return sum(float(record[name]) * weight for name, weight in SCORE_WEIGHTS.items())


def aggregate_scores(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        validate_record(record)
        if record["record_type"] != "answer_score":
            continue
        grouped.setdefault(str(record["config_id"]), []).append(record)
    aggregates: list[dict[str, Any]] = []
    for config_id, group in sorted(grouped.items()):
        count = len(group)
        aggregate = {
            "config_id": config_id,
            "sample_count": count,
            "quality_score": sum(quality_score(row) for row in group) / count,
            "hit_at_5": sum(float(row["hit_at_5"]) for row in group) / count,
            "p95_latency_ms": _p95([float(row["latency_ms"]) for row in group]),
            "mean_tool_calls": sum(float(row["tool_calls"]) for row in group) / count,
            "stale_fact_error_rate": sum(float(row["stale_fact_error"]) for row in group) / count,
        }
        aggregates.append(aggregate)
    pareto_ids = {row["config_id"] for row in pareto_frontier(aggregates)}
    for aggregate in aggregates:
        aggregate["is_pareto"] = aggregate["config_id"] in pareto_ids
    return aggregates


def pareto_frontier(aggregates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    frontier: list[dict[str, Any]] = []
    for candidate in aggregates:
        dominated = any(
            other is not candidate
            and float(other["quality_score"]) >= float(candidate["quality_score"])
            and float(other["hit_at_5"]) >= float(candidate["hit_at_5"])
            and float(other["p95_latency_ms"]) <= float(candidate["p95_latency_ms"])
            and (
                float(other["quality_score"]) > float(candidate["quality_score"])
                or float(other["hit_at_5"]) > float(candidate["hit_at_5"])
                or float(other["p95_latency_ms"]) < float(candidate["p95_latency_ms"])
            )
            for other in aggregates
        )
        if not dominated:
            frontier.append(dict(candidate))
    return sorted(frontier, key=lambda row: (-float(row["quality_score"]), float(row["p95_latency_ms"]), str(row["config_id"])))


def select_top_configs(aggregates: Sequence[Mapping[str, Any]], count: int) -> list[str]:
    if count < 1:
        raise ValueError("count must be positive")
    ranked = sorted(
        aggregates,
        key=lambda row: (
            -float(row["quality_score"]),
            -float(row["hit_at_5"]),
            float(row["p95_latency_ms"]),
            str(row["config_id"]),
        ),
    )
    return [str(row["config_id"]) for row in ranked[:count]]


def feature_promotion_decision(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    baseline_latency = float(baseline["p95_latency_ms"])
    latency_ratio = (
        float(candidate["p95_latency_ms"]) / baseline_latency
        if baseline_latency > 0
        else math.inf
    )
    quality_gain = float(candidate["quality_score"]) - float(baseline["quality_score"])
    hit_gain = float(candidate["hit_at_5"]) - float(baseline["hit_at_5"])
    return {
        "promote": (
            quality_gain + THRESHOLD_EPSILON >= 0.03
            or hit_gain + THRESHOLD_EPSILON >= 0.05
        )
        and latency_ratio <= 1.25 + THRESHOLD_EPSILON,
        "quality_gain": quality_gain,
        "hit_at_5_gain": hit_gain,
        "latency_ratio": latency_ratio,
    }


def freshness_promotion_decision(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    ordinary_quality_baseline: float,
    ordinary_quality_candidate: float,
) -> dict[str, Any]:
    baseline_errors = float(baseline["stale_fact_error_rate"])
    candidate_errors = float(candidate["stale_fact_error_rate"])
    stale_error_reduction = (
        (baseline_errors - candidate_errors) / baseline_errors
        if baseline_errors > 0
        else 0.0
    )
    baseline_latency = float(baseline["p95_latency_ms"])
    latency_ratio = (
        float(candidate["p95_latency_ms"]) / baseline_latency
        if baseline_latency > 0
        else math.inf
    )
    ordinary_quality_drop = ordinary_quality_baseline - ordinary_quality_candidate
    return {
        "promote": (
            stale_error_reduction + THRESHOLD_EPSILON >= 0.25
            and ordinary_quality_drop <= 0.02 + THRESHOLD_EPSILON
            and latency_ratio <= 1.15 + THRESHOLD_EPSILON
        ),
        "stale_error_reduction": stale_error_reduction,
        "ordinary_quality_drop": ordinary_quality_drop,
        "latency_ratio": latency_ratio,
    }


def staged_repeat_schedule(
    questions: Sequence[Mapping[str, Any]],
    first_pass_records: Sequence[Mapping[str, Any]],
    *,
    phase: str,
) -> list[RunSpec]:
    aggregates = aggregate_scores(first_pass_records)
    if phase == "ablation":
        selected = select_top_configs(aggregates, 3)
        question_ids = [str(q["id"]) for q in questions]
    elif phase == "freshness":
        selected = select_top_configs(aggregates, 2)
        question_ids = freshness_question_ids(questions)
    else:
        raise ValueError("phase must be 'ablation' or 'freshness'")
    return make_schedule(question_ids, selected, repeats=(2, 3), phase=phase)


def validate_record(record: Mapping[str, Any]) -> None:
    common = {"schema_version", "record_type", "run_id", "question_id", "config_id", "repeat", "seed"}
    if record.get("schema_version") != 1 or common - set(record):
        raise ValueError("record is missing required common fields or has an unsupported schema")
    if record["record_type"] == "retrieval_run":
        required = {"status", "started_at", "finished_at", "latency_ms", "tool_calls", "results", "failure_detail"}
        if required - set(record) or not isinstance(record["results"], list):
            raise ValueError("invalid retrieval_run record")
    elif record["record_type"] == "answer_score":
        required = {*SCORE_WEIGHTS, "hit_at_5", "latency_ms", "tool_calls", "stale_fact_error", "notes"}
        if required - set(record):
            raise ValueError("invalid answer_score record")
        for field in (*SCORE_WEIGHTS, "hit_at_5"):
            value = record[field]
            if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                raise ValueError(f"{field} must be between 0 and 1")
        if record["stale_fact_error"] not in (0, 1):
            raise ValueError("stale_fact_error must be 0 or 1")
    else:
        raise ValueError(f"unknown record_type: {record.get('record_type')!r}")


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    rows = []
    for record in records:
        validate_record(record)
        rows.append(json.dumps(dict(record), ensure_ascii=False, sort_keys=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at line {line_number}") from error
        if not isinstance(record, dict):
            raise ValueError(f"JSONL line {line_number} must be an object")
        validate_record(record)
        records.append(record)
    return records


def spec_as_dict(spec: RunSpec) -> dict[str, Any]:
    return asdict(spec)
