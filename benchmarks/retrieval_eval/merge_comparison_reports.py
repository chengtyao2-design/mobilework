"""Merge current comparison shards and historical runs into one final report."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from benchmarks.retrieval_eval import multikb
from benchmarks.retrieval_eval.system_comparison import (
    aggregate_runs,
    apply_stale_fact_labels,
    import_historical_path,
    load_manifest,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, action="append", default=[])
    parser.add_argument("--historical", type=Path, action="append", default=[])
    parser.add_argument("--stale-labels", type=Path)
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

    # A later retry shard replaces the earlier current-run record with the same
    # deterministic run id. Historical batches remain separate by provenance.
    deduplicated: list[dict] = []
    current_positions: dict[str, int] = {}
    for row in records:
        if row.get("provenance_label") == "current_run":
            run_id = str(row["run_id"])
            if run_id in current_positions:
                deduplicated[current_positions[run_id]] = row
                continue
            current_positions[run_id] = len(deduplicated)
        deduplicated.append(row)
    records = deduplicated

    # Older shards created before the adapter classified a required-vector
    # degradation as a failed condition still contain the real run and error.
    # Normalize only their status; never alter candidates or metrics.
    for row in records:
        if (row.get("provenance_label") == "current_run" and row.get("system_id") == "mobilework"
                and row.get("status") == "ok" and "vector" in row.get("channels", [])
                and row.get("degradation_detail") and row.get("condition_id") != "embedding_error"):
            row["status"] = "failed"
            row["failure_detail"] = row.get("failure_detail") or "; ".join(map(str, row["degradation_detail"]))

    applied = apply_stale_fact_labels(records, args.stale_labels) if args.stale_labels else 0
    for system_id, system in systems_by_id.items():
        current_rows = [row for row in records if row.get("provenance_label") == "current_run" and row.get("system_id") == system_id]
        ok_count = sum(row.get("status") == "ok" for row in current_rows)
        if current_rows and ok_count == len(current_rows):
            system["status"] = "completed"
        elif ok_count:
            system["status"] = "partial"
        else:
            system["status"] = "unavailable"
        system["notes"] = f"current runs: {len(current_rows)}; ok: {ok_count}; failed/unavailable: {len(current_rows) - ok_count}"
    failures = [{key: row.get(key) for key in (
        "run_id", "experiment_id", "system_id", "question_id", "condition_id", "status",
        "degradation_detail", "failure_detail", "provenance_origin")}
        for row in records if row.get("status") != "ok"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = {
        "schema_version": 1,
        "run_summary": {
            "run_id": args.output_dir.name,
            "started_at": min((row["started_at"] for row in records), default=now),
            "finished_at": now,
            "run_count": len(records),
            "current_run_count": sum(row.get("provenance_label") == "current_run" for row in records),
            "historical_run_count": sum(row.get("provenance_label") == "historical_result" for row in records),
            "evaluator_mode": "deterministic_lexical_proxy_v1 plus manual stale-fact labels",
            "note": "Only actual adapter and imported historical records are included; failures are not imputed.",
        },
        "systems": list(systems_by_id.values()),
        "questions": multikb.load_questions(),
        "experiment_manifest": manifest,
        "raw_runs": records,
        "aggregate": aggregate_runs(records),
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
