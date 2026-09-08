from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.retrieval_eval import multikb


def score_record(
    config_id: str,
    *,
    quality: float,
    hit_at_5: float,
    latency_ms: float,
    question_id: str = "Q01",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "answer_score",
        "run_id": f"ablation-{config_id}-{question_id}-r1",
        "question_id": question_id,
        "config_id": config_id,
        "repeat": 1,
        "seed": 20260904,
        "factual_correctness": quality,
        "citation_recall": quality,
        "grounded_ratio": quality,
        "conflict_handling": quality,
        "abstention_score": quality,
        "hit_at_5": hit_at_5,
        "latency_ms": latency_ms,
        "tool_calls": 2,
        "stale_fact_error": 0,
        "notes": "synthetic",
    }


def test_question_manifest_has_exact_agreed_shape_and_freshness_subset():
    questions = multikb.load_questions()

    assert [question["id"] for question in questions] == [f"Q{number:02d}" for number in range(1, 21)]
    assert all(set(question) == multikb.QUESTION_FIELDS for question in questions)
    assert multikb.freshness_question_ids(questions) == [
        "Q11",
        "Q12",
        "Q13",
        "Q14",
        "Q16",
        "Q17",
        "Q18",
        "Q19",
    ]
    assert questions[-1]["answerable"] is False
    assert "明确拒绝给出百分比" in questions[-1]["required_facts"]


def test_config_manifest_defines_nine_ablations_and_four_freshness_modes():
    configs = multikb.load_configs()

    assert tuple(config["id"] for config in configs["ablations"]) == multikb.ABLATION_IDS
    assert tuple(config["id"] for config in configs["freshness"]) == multikb.FRESHNESS_IDS
    assert [config["retrieval"]["claim_freshness_mode"] for config in configs["freshness"]] == [
        "off",
        "context",
        "rerank",
        "both",
    ]
    assert configs["ablations"][6]["retrieval"]["scope"] == "wiki"
    assert configs["ablations"][7]["retrieval"]["scope"] == "raw"
    assert configs["ablations"][8]["retrieval"]["raw_evidence_fallback"] is True


def test_initial_and_staged_schedules_have_deterministic_cardinality_and_seeds():
    questions = multikb.load_questions()
    initial = multikb.initial_ablation_schedule(questions)

    assert len(initial) == 9 * 20
    assert len({spec.run_id for spec in initial}) == len(initial)
    assert initial == multikb.initial_ablation_schedule(questions)

    first_pass = [
        score_record("C01", quality=0.70, hit_at_5=0.8, latency_ms=100),
        score_record("C02", quality=0.90, hit_at_5=0.8, latency_ms=300),
        score_record("C03", quality=0.90, hit_at_5=0.9, latency_ms=400),
        score_record("C04", quality=0.80, hit_at_5=1.0, latency_ms=200),
    ]
    repeats = multikb.staged_repeat_schedule(questions, first_pass, phase="ablation")

    assert len(repeats) == 3 * 20 * 2
    assert {spec.config_id for spec in repeats} == {"C02", "C03", "C04"}
    assert {spec.repeat for spec in repeats} == {2, 3}


def test_freshness_schedule_runs_eight_questions_then_repeats_top_two():
    questions = multikb.load_questions()
    initial = multikb.initial_freshness_schedule(questions)
    first_pass = [
        score_record("F0", quality=0.60, hit_at_5=0.7, latency_ms=100, question_id="Q11"),
        score_record("F1", quality=0.75, hit_at_5=0.8, latency_ms=120, question_id="Q11"),
        score_record("F2", quality=0.85, hit_at_5=0.8, latency_ms=140, question_id="Q11"),
        score_record("F3", quality=0.80, hit_at_5=0.9, latency_ms=160, question_id="Q11"),
    ]
    repeats = multikb.staged_repeat_schedule(questions, first_pass, phase="freshness")

    assert len(initial) == 4 * 8
    assert len(repeats) == 2 * 8 * 2
    assert {spec.config_id for spec in repeats} == {"F2", "F3"}
    assert {spec.question_id for spec in repeats} == set(multikb.freshness_question_ids(questions))


def test_weighted_quality_aggregation_p95_and_pareto_are_deterministic():
    records = [
        score_record("C01", quality=0.7, hit_at_5=0.7, latency_ms=100, question_id="Q01"),
        score_record("C01", quality=0.9, hit_at_5=0.9, latency_ms=200, question_id="Q02"),
        score_record("C02", quality=0.7, hit_at_5=0.7, latency_ms=250, question_id="Q01"),
        score_record("C03", quality=0.9, hit_at_5=0.9, latency_ms=300, question_id="Q01"),
    ]

    aggregates = multikb.aggregate_scores(records)
    by_id = {row["config_id"]: row for row in aggregates}

    assert by_id["C01"]["quality_score"] == pytest.approx(0.8)
    assert by_id["C01"]["p95_latency_ms"] == 200
    assert by_id["C02"]["is_pareto"] is False
    assert {row["config_id"] for row in multikb.pareto_frontier(aggregates)} == {"C01", "C03"}


def test_promotion_thresholds_encode_quality_latency_and_freshness_gates():
    baseline = {
        "quality_score": 0.70,
        "hit_at_5": 0.80,
        "p95_latency_ms": 1000,
        "stale_fact_error_rate": 0.40,
    }
    candidate = {
        "quality_score": 0.73,
        "hit_at_5": 0.80,
        "p95_latency_ms": 1250,
        "stale_fact_error_rate": 0.30,
    }

    assert multikb.feature_promotion_decision(baseline, candidate)["promote"] is True
    assert multikb.feature_promotion_decision(
        baseline, dict(candidate, p95_latency_ms=1251)
    )["promote"] is False
    freshness = multikb.freshness_promotion_decision(
        baseline,
        dict(candidate, p95_latency_ms=1150),
        ordinary_quality_baseline=0.80,
        ordinary_quality_candidate=0.78,
    )
    assert freshness["stale_error_reduction"] == pytest.approx(0.25)
    assert freshness["promote"] is True
    assert multikb.freshness_promotion_decision(
        baseline,
        dict(candidate, stale_fact_error_rate=0.31, p95_latency_ms=1150),
        ordinary_quality_baseline=0.80,
        ordinary_quality_candidate=0.78,
    )["promote"] is False


def test_jsonl_round_trip_and_record_validation(tmp_path: Path):
    destination = tmp_path / "records.jsonl"
    records = [score_record("C01", quality=0.8, hit_at_5=1.0, latency_ms=123)]

    multikb.write_jsonl(destination, records)

    assert multikb.read_jsonl(destination) == records
    invalid = dict(records[0], factual_correctness=1.1)
    with pytest.raises(ValueError, match="between 0 and 1"):
        multikb.validate_record(invalid)


def test_checked_in_json_schema_is_valid_json_and_covers_both_record_types():
    schema = json.loads(multikb.SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$schema"].endswith("2020-12/schema")
    assert {entry["$ref"] for entry in schema["oneOf"]} == {
        "#/$defs/retrieval_run",
        "#/$defs/answer_score",
    }
