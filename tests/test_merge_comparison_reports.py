from __future__ import annotations

from benchmarks.retrieval_eval.merge_comparison_reports import (
    active_records,
    annotate_attempts,
    failure_rows,
    import_nashsu_no_vector_ablation,
)


def record(run_id: str, status: str, *, provenance: str = "current_run") -> dict:
    return {
        "run_id": run_id,
        "status": status,
        "provenance_label": provenance,
        "experiment_id": "system_retrieval",
        "system_id": "mobilework",
        "question_id": "Q01",
        "condition_id": "native_top5",
        "failure_detail": "timeout" if status != "ok" else "",
    }


def test_successful_retry_preserves_failed_attempt_and_becomes_active():
    rows = annotate_attempts([record("run-1", "failed"), record("run-1", "ok")])

    assert [row["attempt"] for row in rows] == [1, 2]
    assert rows[0]["attempt_id"] == "current_run:run-1:attempt-1"
    assert rows[0]["superseded"] is True
    assert rows[0]["superseded_by"] == rows[1]["attempt_id"]
    assert rows[1]["superseded"] is False
    assert rows[1]["superseded_by"] is None
    assert active_records(rows) == [rows[1]]

    failures = failure_rows(rows)
    assert len(failures) == 1
    assert failures[0]["attempt_id"] == rows[0]["attempt_id"]
    assert failures[0]["superseded"] is True
    assert failures[0]["superseded_by"] == rows[1]["attempt_id"]


def test_later_failed_retry_cannot_replace_an_existing_success():
    rows = annotate_attempts([record("run-1", "ok"), record("run-1", "failed")])

    assert active_records(rows) == [rows[0]]
    assert rows[0]["superseded"] is False
    assert rows[1]["superseded"] is True
    assert rows[1]["superseded_by"] == rows[0]["attempt_id"]


def test_latest_attempt_is_active_when_none_succeeded():
    rows = annotate_attempts([
        record("run-1", "failed"),
        record("run-1", "unavailable"),
    ])

    assert rows[0]["superseded"] is True
    assert rows[0]["superseded_by"] == rows[1]["attempt_id"]
    assert active_records(rows) == [rows[1]]


def test_historical_records_are_not_superseded_by_current_retry():
    rows = annotate_attempts([
        record("shared-id", "failed", provenance="historical_result"),
        record("shared-id", "ok", provenance="current_run"),
    ])

    assert len(active_records(rows)) == 2
    assert rows[0]["attempt_id"].startswith("historical_result:")
    assert rows[0]["superseded"] is False
    assert rows[0]["superseded_by"] is None


def test_legacy_rows_without_attempt_fields_and_preannotated_rows_are_normalized():
    legacy = record("run-1", "failed")
    preannotated = record("run-1", "ok") | {
        "attempt_id": "stale-id",
        "attempt": 99,
        "superseded": True,
        "superseded_by": "stale-winner",
    }

    rows = annotate_attempts([legacy, preannotated])

    assert [row["attempt"] for row in rows] == [1, 2]
    assert rows[1]["attempt_id"] == "current_run:run-1:attempt-2"
    assert rows[1]["superseded"] is False
    assert rows[1]["superseded_by"] is None


def test_old_nashsu_rows_are_preserved_as_named_historical_ablation(tmp_path):
    path = tmp_path / "comparison-report.json"
    path.write_text(__import__("json").dumps({"raw_runs": [
        record("old-search", "ok") | {"system_id": "nashsu", "adapter_metadata": {"vector_hits": 0}},
        record("other", "ok"),
    ]}), encoding="utf-8")

    rows = import_nashsu_no_vector_ablation(path)

    assert len(rows) == 1
    assert rows[0]["run_id"] == "nashsu-token-graph-old-search"
    assert rows[0]["experiment_id"] == "nashsu_no_vector_ablation"
    assert rows[0]["condition_id"] == "nashsu_token_graph"
    assert rows[0]["provenance_label"] == "historical_result"
    assert rows[0]["adapter_metadata"]["embedding_enabled"] is False
