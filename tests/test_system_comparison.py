from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from benchmarks.retrieval_eval import multikb
from benchmarks.retrieval_eval.system_comparison import (
    AdapterOutcome,
    ComparisonSpec,
    KarpathyOpenCodeAdapter,
    MobileworkAdapter,
    NashsuHttpAdapter,
    aggregate_runs,
    apply_stale_fact_labels,
    compute_metrics,
    import_historical_path,
    import_historical_report,
    load_manifest,
    make_schedule,
    mobilework_trace_metadata,
    normalize_results,
    parse_opencode_events,
    run_spec,
)


def spec(*, system="mobilework", mode="retrieval", condition=None) -> ComparisonSpec:
    return ComparisonSpec("run-1", "system_retrieval", system, "Q01", "standard",
                          "native_top5", 1, 20260907, mode, condition or {})


def test_manifest_has_all_agreed_experiment_subsets_and_deterministic_schedule():
    manifest = load_manifest()
    experiments = manifest["experiments"]

    assert experiments["system_retrieval"]["question_ids"] == [
        "Q01", "Q03", "Q04", "Q06", "Q07", "Q08", "Q10", "Q12", "Q15", "Q18", "Q19", "Q20"
    ]
    assert experiments["system_answer"]["question_ids"] == ["Q01", "Q08", "Q15", "Q18", "Q19", "Q20"]
    assert set(manifest["fuzzy_queries"]) == {"Q01", "Q03", "Q06", "Q07", "Q08", "Q19"}
    assert experiments["routing"]["question_ids"] == ["Q09", "Q12", "Q13", "Q15", "Q16", "Q17", "Q18", "Q19"]
    schedule = make_schedule(manifest)
    assert len(schedule) == 108 + 36 + 144 + 72 + 18 + 5
    assert schedule == make_schedule(manifest)
    assert len({item.run_id for item in schedule}) == len(schedule)


def test_normalized_metrics_use_path_suffixes_and_report_irrelevant_kb():
    question = multikb.load_questions()[0]
    expected_path = question["expected_sources"][0].split(":", 1)[1]
    results = normalize_results([
        {"kb_id": "kb_research", "path": "wiki/irrelevant.md", "score": 0.9},
        {"kb_id": "kb_enterprise", "path": expected_path, "global_score": 0.8},
    ], 5)
    metrics = compute_metrics(question, results)

    assert metrics["hit_at_5"] == 1
    assert metrics["mrr"] == pytest.approx(0.5)
    assert metrics["irrelevant_evidence_rate"] == pytest.approx(0.5)
    assert metrics["citation_recall"] is None


def test_mobilework_adapter_passes_real_routing_controls_to_fake_retriever(tmp_path: Path):
    calls = []

    def retrieve(**kwargs):
        calls.append(kwargs)
        return {"results": [{"kb_id": "kb_enterprise", "path": "wiki/a.md", "title": "A"}],
                "queried_kb_ids": ["kb_enterprise"], "routing": {"selected_kb_ids": ["kb_enterprise"]}, "errors": {}}

    adapter = MobileworkAdapter(tmp_path, retrieve_fn=retrieve)
    question = multikb.load_questions()[0]
    outcome = adapter.run(question["question"], question,
                          spec(condition={"channels": ["keyword"], "routing": "gold"}), 5)

    assert outcome.status == "ok"
    assert calls[0]["channels"] == ["keyword"]
    assert calls[0]["kb_ids"] == question["target_kbs"]
    assert outcome.results[0]["source"] == "kb_enterprise:wiki/a.md"


def test_nashsu_adapter_parses_search_and_records_service_unavailable(tmp_path: Path):
    calls = []

    def request(path, payload):
        calls.append((path, payload))
        return {"ok": True, "mode": "hybrid", "tokenHits": 2,
                "results": [{"path": "wiki/a.md", "title": "A", "snippet": "evidence", "score": .7}]}

    adapter = NashsuHttpAdapter("http://example.test", request_fn=request, repository_path=tmp_path)
    question = multikb.load_questions()[0]
    outcome = adapter.run(question["question"], question, spec(system="nashsu"), 5)
    assert outcome.status == "ok"
    assert calls[0][0] == "/api/v1/projects/current/search"
    assert outcome.results[0]["path"] == "wiki/a.md"

    unavailable = NashsuHttpAdapter("http://example.test", request_fn=lambda *_: (_ for _ in ()).throw(URLError("offline")))
    assert unavailable.run("q", question, spec(system="nashsu"), 5).status == "unavailable"


def test_opencode_parser_reads_tool_state_and_missing_karpathy_is_unavailable(tmp_path: Path):
    output = "\n".join([
        json.dumps({"type": "tool_use", "part": {"type": "tool", "state": {"output": json.dumps({
            "results": [{"path": "wiki/a.md", "title": "A", "content": "proof", "score": 1}]
        })}}}),
        json.dumps({"type": "text", "part": {"text": "answer"}}),
    ])
    results, answer, citations, calls = parse_opencode_events(output, 5)
    assert (results[0]["path"], answer, citations, calls) == ("wiki/a.md", "answer", ["wiki/a.md"], 1)

    missing = KarpathyOpenCodeAdapter(tmp_path / "missing")
    assert missing.run("q", multikb.load_questions()[0], spec(system="karpathy"), 5).status == "unavailable"


def test_skill_trace_records_actual_channels_and_budget_check():
    output = json.dumps({"type": "tool_use", "part": {"tool": "wiki-retrieval_retrieve", "state": {
        "output": json.dumps({"kb_status": {"kb_enterprise": {"channels": ["vector", "keyword"]}}})
    }}})
    trace = mobilework_trace_metadata(output, "balanced")
    assert trace["retrieval_calls"] == 1
    assert trace["actual_channels"] == [["vector", "keyword"]]
    assert trace["profile_constraints_passed"] is True


def test_run_spec_and_aggregate_preserve_failed_rows_without_fake_metrics(tmp_path: Path):
    class FakeAdapter:
        system_id = "mobilework"
        system_name = "Mobilework"
        repository = "local"
        commit = "commit-a"
        corpus_wiki_pages = 50

        def run(self, *_args, **_kwargs):
            return AdapterOutcome("unavailable", failure_detail="service stopped")

    question = multikb.load_questions()[0]
    row = run_spec(FakeAdapter(), question, spec(), question["question"], 5)
    assert row["status"] == "unavailable"
    assert row["provenance_label"] == "current_run"
    assert row["results"] == []
    aggregate = aggregate_runs([row])[0]
    assert aggregate["ok_count"] == 0
    assert aggregate["hit_at_5"] is None


def test_historical_import_adds_explicit_provenance_and_keeps_commit(tmp_path: Path):
    question = multikb.load_questions()[0]
    report = {
        "run_summary": {"run_id": "old-run", "git_commit": "old-commit", "started_at": "2026-09-04T00:00:00+00:00", "finished_at": "2026-09-04T00:00:01+00:00"},
        "questions": [question],
        "retrieval_runs": [{"run_id": "ablation-C02-Q01-r1", "question_id": "Q01", "config_id": "C02", "repeat": 1,
                            "seed": 1, "status": "ok", "latency_ms": 12, "tool_calls": 1, "results": [], "failure_detail": ""}],
        "answer_scores": [],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    rows = import_historical_report(path)
    assert rows[0]["provenance_label"] == "historical_result"
    assert rows[0]["provenance_origin"] == "old-run"
    assert rows[0]["system_commit"] == "old-commit"
    assert "sha256" not in json.dumps(rows[0])


def test_terminal_profile_directory_import_is_labeled_as_historical(tmp_path: Path):
    payload = {"profile": "fast", "question": "PBC 包含哪三类目标？", "passed": False,
               "seconds": 1.25, "answer": "", "tool_calls": [], "actual_channels": [["vector"]],
               "error": "pre-refactor snapshot failed"}
    (tmp_path / "fast.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    rows = import_historical_path(tmp_path)
    assert len(rows) == 1
    assert rows[0]["experiment_id"] == "historical_skill_regression"
    assert rows[0]["provenance_label"] == "historical_result"
    assert rows[0]["channels"] == ["vector"]
    assert rows[0]["status"] == "failed"


def test_manual_stale_labels_apply_only_to_reviewed_question_and_condition(tmp_path: Path):
    rows = [{"question_id": "Q12", "condition_id": "F2", "provenance_origin": "reviewed-batch",
             "metrics": {"stale_fact_error": None},
             "adapter_metadata": {}}]
    labels = {"schema_version": 1, "reviewed_at": "2026-09-07", "provenance_id": "reviewed-batch",
              "labels": {"Q12": {"F2": 1, "rationale": "reviewed"}}}
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")

    assert apply_stale_fact_labels(rows, path) == 1
    assert rows[0]["metrics"]["stale_fact_error"] == 1
    assert rows[0]["adapter_metadata"]["stale_fact_label"]["rationale"] == "reviewed"
