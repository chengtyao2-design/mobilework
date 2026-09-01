from __future__ import annotations

import pytest

from wiki_maintainer.cli import (
    _compact_event,
    _ensure_json_events,
    _event_progress,
    _pending_chunk,
    _pending_summary,
)


def test_pending_summary_omits_chunk_bodies_and_ids() -> None:
    work = {
        "batch_id": "batch-1",
        "events": [
            {
                "kind": "new",
                "source_id": "src_1",
                "path": "raw/sources/a.md",
                "chunks": [
                    {
                        "chunk_id": "chk_1",
                        "sha256": "abc",
                        "ordinal": 0,
                        "chars": 4,
                        "preview": "body",
                        "text": "body",
                    }
                ],
                "diff": {"added_count": 1, "added": [{"chunk_id": "chk_1", "text": "body"}]},
            }
        ],
    }

    summary = _pending_summary(work)

    assert summary["events"][0]["chunk_count"] == 1
    assert "chunks" not in summary["events"][0]
    assert "added_chunk_ids" not in summary["events"][0]["diff"]


def test_compact_event_keeps_provenance_without_chunk_text() -> None:
    event = {
        "source_id": "src_1",
        "chunks": [{"chunk_id": "chk_1", "ordinal": 0, "chars": 4, "preview": "body", "text": "body"}],
        "diff": {"added": [{"chunk_id": "chk_1", "text": "body"}]},
    }

    compact = _compact_event(event, include_chunks=True)

    assert compact["diff"]["added_chunk_ids"] == ["chk_1"]
    assert compact["chunks"] == [{"chunk_id": "chk_1", "ordinal": 0, "chars": 4}]


def test_pending_chunk_returns_only_requested_body() -> None:
    event = {
        "kind": "new",
        "source_id": "src_1",
        "path": "raw/sources/a.md",
        "chunks": [
            {"chunk_id": "chk_1", "ordinal": 0, "text": "first"},
            {"chunk_id": "chk_2", "ordinal": 1, "text": "second"},
        ],
    }

    selected = _pending_chunk(event, 1)

    assert selected["source_id"] == "src_1"
    assert selected["chunk"] == {"chunk_id": "chk_2", "ordinal": 1, "text": "second"}
    assert "chunks" not in selected
    with pytest.raises(ValueError, match="chunk ordinal 2"):
        _pending_chunk(event, 2)


def test_ensure_json_events_adds_opencode_stream_format_once() -> None:
    command = ["opencode", "run", "--dir", ".", "prompt"]

    assert _ensure_json_events(command)
    assert command == ["opencode", "run", "--format", "json", "--dir", ".", "prompt"]
    assert _ensure_json_events(command)
    assert command.count("--format") == 1


def test_event_progress_reports_invalid_tool_call() -> None:
    event = {
        "type": "tool_use",
        "part": {
            "type": "tool",
            "tool": "invalid",
            "state": {
                "status": "completed",
                "input": {"tool": "unknown", "error": "Tool execution aborted"},
            },
        },
    }

    label, error, final_text = _event_progress(event)

    assert label == "Using unknown"
    assert error == "Tool execution aborted"
    assert final_text is None
