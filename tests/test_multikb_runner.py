from copy import deepcopy
import json

import pytest

from benchmarks.retrieval_eval import multikb
from benchmarks.retrieval_eval.run_multikb import extract_answer, run_one, run_evaluation


def test_extractive_answer_never_invents_gold_facts():
    answer, citations = extract_answer("弹性福利", [{"kb_id": "a", "path": "x", "content": "弹性福利改善满意度。"}], 100)
    assert answer == "弹性福利改善满意度。"
    assert citations == ["a:x"]
    assert "流失率" not in answer


def test_disabled_vector_never_invokes_vector_and_records_degradation(tmp_path):
    question = multikb.load_questions()[0]
    config = multikb.load_configs()["ablations"][0]
    spec = multikb.initial_ablation_schedule([question])[0]
    calls = []
    def retrieve(**kwargs):
        calls.append(kwargs)
        return {"results": []}
    run, score = run_one(tmp_path, question, config, spec, retrieve, {"keyword", "graph"})
    assert all("vector" not in call["channels"] for call in calls)
    assert run["degradations"]
    assert score["stale_fact_error_measured"] is False


def test_call_budget_and_no_gold_kb_routing(tmp_path):
    question = multikb.load_questions()[0]
    config = deepcopy(multikb.load_configs()["ablations"][0])
    config["retrieval"].update(decompose=True, raw_evidence_fallback=True)
    config["budget"]["max_tool_calls"] = 1
    calls = []
    def retrieve(**kwargs):
        calls.append(kwargs)
        return {"results": []}
    run, _ = run_one(tmp_path, question, config, multikb.initial_ablation_schedule([question])[0], retrieve)
    assert len(calls) == 1
    assert "kb_ids" not in calls[0]
    assert run["stop_reason"] == "max_tool_calls"


def test_full_schedule_retained_and_never_promotes_proxy(tmp_path):
    report = run_evaluation(tmp_path, tmp_path / "results", retrieve_fn=lambda **kwargs: {"results": []}, allowed_channels={"keyword"})
    assert len(report["retrieval_runs"]) == 364
    assert all(row["promote"] is False for row in report["aggregate"])
    assert all(row["stale_fact_error_rate"] is None for row in report["freshness_ablation"])
    assert len((tmp_path / "results/retrieval_runs.jsonl").read_text(encoding="utf-8").splitlines()) == 364
    with pytest.raises(FileExistsError):
        run_evaluation(tmp_path, tmp_path / "results", retrieve_fn=lambda **kwargs: {})


def test_schema_accepts_freshness_ids():
    import re
    from pathlib import Path
    # Locate the shared config definition without requiring a JSON Schema runtime.
    pattern = next(line for line in Path("benchmarks/retrieval_eval/schemas/multikb-eval-record.v1.schema.json").read_text().splitlines() if '"config_id": {' in line)
    value = json.loads("{" + pattern.strip().rstrip(",") + "}")["config_id"]["pattern"]
    assert all(re.fullmatch(value, key) for key in ("C01", "C09", "F0", "F3"))
