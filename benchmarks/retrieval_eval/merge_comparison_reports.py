"""Merge current comparison shards and historical runs into one final report."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from benchmarks.retrieval_eval import multikb
from benchmarks.retrieval_eval.system_comparison import (
    aggregate_runs,
    apply_stale_fact_labels,
    import_historical_path,
    load_manifest,
    write_report,
)


def annotate_attempts(records: Iterable[dict]) -> list[dict]:
    """Add retry metadata while retaining every observed record.

    Current-run records compete only with other current-run records sharing the
    same deterministic run id.  The latest successful attempt is active; when
    no attempt succeeded, the latest attempt is active.  Historical imports
    remain independent evidence and are never superseded by current retries.

    Attempt metadata is regenerated from input order so reports produced before
    these fields existed can be merged with retry shards without migration.
    """
    annotated = [dict(row) for row in records]
    counters: dict[tuple[str, str], int] = defaultdict(int)
    current_groups: dict[str, list[int]] = defaultdict(list)

    for index, row in enumerate(annotated):
        provenance = str(row.get("provenance_label") or "current_run")
        run_id = str(row["run_id"])
        key = provenance, run_id
        counters[key] += 1
        attempt = counters[key]
        row["attempt"] = attempt
        row["attempt_id"] = f"{provenance}:{run_id}:attempt-{attempt}"
        row["superseded"] = False
        row["superseded_by"] = None
        if provenance == "current_run":
            current_groups[run_id].append(index)

    for indices in current_groups.values():
        successful = [index for index in indices if annotated[index].get("status") == "ok"]
        active_index = successful[-1] if successful else indices[-1]
        active_attempt_id = annotated[active_index]["attempt_id"]
        for index in indices:
            if index != active_index:
                annotated[index]["superseded"] = True
                annotated[index]["superseded_by"] = active_attempt_id

    return annotated


def active_records(records: Iterable[dict]) -> list[dict]:
    """Return records selected for aggregates and current system status."""
    return [row for row in records if not row.get("superseded", False)]


def failure_rows(records: Iterable[dict]) -> list[dict]:
    """Keep failed attempts in the audit trail, including supersession state."""
    keys = (
        "run_id", "attempt_id", "attempt", "superseded", "superseded_by",
        "experiment_id", "system_id", "question_id", "condition_id", "status",
        "degradation_detail", "failure_detail", "provenance_origin",
    )
    return [{key: row.get(key) for key in keys} for row in records if row.get("status") != "ok"]


def import_nashsu_no_vector_ablation(path: Path) -> list[dict]:
    """Preserve the original token+graph runs as a separately named ablation."""
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for source in report.get("raw_runs", []):
        if source.get("system_id") != "nashsu" or source.get("experiment_id") not in ("system_retrieval", "system_answer"):
            continue
        row = dict(source)
        row["run_id"] = f"nashsu-token-graph-{source['run_id']}"
        row["experiment_id"] = "nashsu_no_vector_ablation"
        row["condition_id"] = "nashsu_token_graph"
        row["provenance_label"] = "historical_result"
        row["provenance_origin"] = f"{path.parent.name}:nashsu-token-graph"
        metadata = dict(row.get("adapter_metadata") or {})
        metadata.update({"embedding_enabled": False, "ablation": "token_graph"})
        row["adapter_metadata"] = metadata
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, action="append", default=[])
    parser.add_argument("--historical", type=Path, action="append", default=[])
    parser.add_argument("--stale-labels", type=Path)
    parser.add_argument("--nashsu-no-vector-report", type=Path,
        help="old comparison report whose nashsu rows should be retained as token+graph ablation")
    parser.add_argument("--sources", type=Path, default=Path(__file__).with_name("baseline_sources.v1.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    manifest = load_manifest()
    systems_by_id = {row["id"]: dict(row) for row in manifest["systems"]}
    source_metadata = json.loads(args.sources.read_text(encoding="utf-8")).get("systems", {})
    records: list[dict] = []
    input_reports = []
    for path in args.report:
        report = json.loads(path.read_text(encoding="utf-8"))
        shard_rows = list(report.get("raw_runs", []))
        records.extend(shard_rows)
        input_reports.append({"path": str(path), "record_count": len(shard_rows), "kind": "current_run"})
        for system in report.get("systems", []):
            current = systems_by_id.setdefault(system["id"], {})
            for key, value in system.items():
                if value not in (None, "", "unknown"):
                    current[key] = value
    for system_id, metadata in source_metadata.items():
        systems_by_id.setdefault(system_id, {}).update(metadata)

    historical_imports = []
    for path in args.historical:
        rows = import_historical_path(path)
        records.extend(rows)
        historical_imports.append({"path": str(path), "record_count": len(rows), "provenance_label": "historical_result"})
    if args.nashsu_no_vector_report:
        rows = import_nashsu_no_vector_ablation(args.nashsu_no_vector_report)
        records.extend(rows)
        historical_imports.append({"path": str(args.nashsu_no_vector_report),
            "record_count": len(rows), "provenance_label": "historical_result",
            "use": "nashsu_token_graph_ablation"})

    # Older shards created before the adapter classified a required-vector
    # degradation as a failed condition still contain the real run and error.
    # Normalize only their status; never alter candidates or metrics.
    for row in records:
        if (row.get("provenance_label") == "current_run" and row.get("system_id") == "mobilework"
                and row.get("status") == "ok" and "vector" in row.get("channels", [])
                and row.get("degradation_detail") and row.get("condition_id") != "embedding_error"):
            row["status"] = "failed"
            row["failure_detail"] = row.get("failure_detail") or "; ".join(map(str, row["degradation_detail"]))

    records = annotate_attempts(records)
    active = active_records(records)
    applied = apply_stale_fact_labels(records, args.stale_labels) if args.stale_labels else 0
    for system_id, system in systems_by_id.items():
        current_rows = [row for row in active if row.get("provenance_label") == "current_run" and row.get("system_id") == system_id]
        ok_count = sum(row.get("status") == "ok" for row in current_rows)
        if current_rows and ok_count == len(current_rows):
            system["status"] = "completed"
        elif ok_count:
            system["status"] = "partial"
        else:
            system["status"] = "unavailable"
        attempt_count = sum(row.get("provenance_label") == "current_run" and row.get("system_id") == system_id for row in records)
        system["notes"] = (f"active current runs: {len(current_rows)}; attempts: {attempt_count}; "
                           f"ok: {ok_count}; failed/unavailable: {len(current_rows) - ok_count}")
    failures = failure_rows(records)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = {
        "schema_version": 1,
        "run_summary": {
            "run_id": args.output_dir.name,
            "started_at": min((row["started_at"] for row in records), default=now),
            "finished_at": now,
            "run_count": len(records),
            "active_run_count": len(active),
            "superseded_run_count": len(records) - len(active),
            "current_run_count": sum(row.get("provenance_label") == "current_run" for row in records),
            "active_current_run_count": sum(row.get("provenance_label") == "current_run" for row in active),
            "historical_run_count": sum(row.get("provenance_label") == "historical_result" for row in records),
            "evaluator_mode": "deterministic_lexical_proxy_v1 plus manual stale-fact labels",
            "note": "Only actual adapter and imported historical records are included; failures are not imputed.",
        },
        "systems": list(systems_by_id.values()),
        "questions": multikb.load_questions(),
        "experiment_manifest": manifest,
        "raw_runs": records,
        "aggregate": aggregate_runs(active),
        "failures": failures,
        "input_reports": input_reports,
        "historical_imports": historical_imports,
        "stale_labels": {"path": str(args.stale_labels) if args.stale_labels else None, "applied_count": applied},
    }
    write_report(args.output_dir / "comparison-report.json", report)
    print(json.dumps({"report": str(args.output_dir / "comparison-report.json"), **report["run_summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
