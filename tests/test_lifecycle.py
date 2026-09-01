from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wiki_maintainer.core import (
    atomic_json,
    checkout_page,
    commit,
    initialize,
    load_json,
    paths_for,
    prepare,
    record_source,
    restore_trash,
    rewrite_moved_paths,
    status,
)


def page_text(title: str, source_ids: list[str], sources: list[str], body: str = "Knowledge.") -> str:
    return "\n".join(
        [
            "---",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            'category: "concepts"',
            'tags: ["test"]',
            "aliases: []",
            "wiki_managed: true",
            f"source_ids: {json.dumps(source_ids, ensure_ascii=False)}",
            f"sources: {json.dumps(sources, ensure_ascii=False)}",
            f"summary: {json.dumps(body, ensure_ascii=False)}",
            "base_confidence: 0.5",
            'lifecycle: "draft"',
            'lifecycle_changed: "2026-08-17"',
            'tier: "supporting"',
            'created: "2026-08-17T00:00:00+00:00"',
            'updated: "2026-08-17T00:00:00+00:00"',
            "---",
            "",
            f"# {title}",
            "",
            body,
            "",
        ]
    )


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _source(self, name: str, text: str) -> Path:
        path = self.root / "raw" / "sources" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _stage(self, work: dict, rel: str, text: str) -> Path:
        path = self.root / work["staging_wiki"] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _ingest_one(self, name: str = "alpha.md") -> tuple[str, str]:
        self._source(name, "Alpha is a durable concept.")
        work = prepare(self.root)
        assert work is not None
        event = work["events"][0]
        rel = "concepts/alpha.md"
        self._stage(work, rel, page_text("Alpha", [event["source_id"]], [event["path"]]))
        record_source(self.root, work["batch_id"], event["source_id"], [rel])
        commit(self.root, work["batch_id"])
        return event["source_id"], rel

    def test_new_unchanged_and_modified(self) -> None:
        source_id, rel = self._ingest_one()
        self.assertEqual(status(self.root)["active_sources"], 1)
        self.assertIsNone(prepare(self.root))

        self._source("alpha.md", "Alpha changed materially.")
        work = prepare(self.root)
        assert work is not None
        self.assertEqual(work["events"][0]["kind"], "modified")
        self._stage(
            work,
            rel,
            page_text("Alpha", [source_id], ["raw/sources/alpha.md"], "Alpha changed materially."),
        )
        record_source(self.root, work["batch_id"], source_id, [rel])
        result = commit(self.root, work["batch_id"])
        self.assertEqual(result["pages_upserted"], 1)
        self.assertIn("changed materially", (self.root / "wiki" / rel).read_text(encoding="utf-8"))

    def test_delete_single_source_archives_page(self) -> None:
        _, rel = self._ingest_one()
        (self.root / "raw" / "sources" / "alpha.md").unlink()
        work = prepare(self.root)
        assert work is not None
        self.assertEqual(work["events"][0]["kind"], "deleted")
        (self.root / work["staging_wiki"] / rel).unlink()
        result = commit(self.root, work["batch_id"])
        self.assertEqual(result["pages_archived"], 1)
        self.assertFalse((self.root / "wiki" / rel).exists())
        self.assertTrue((self.root / ".wiki-trash" / work["batch_id"] / rel).exists())

    def test_delete_shared_source_requires_semantic_reconcile(self) -> None:
        self._source("a.md", "A supports shared knowledge.")
        self._source("b.md", "B also supports shared knowledge.")
        work = prepare(self.root)
        assert work is not None
        events = {event["path"]: event for event in work["events"]}
        a = events["raw/sources/a.md"]
        b = events["raw/sources/b.md"]
        rel = "concepts/shared.md"
        self._stage(work, rel, page_text("Shared", [a["source_id"], b["source_id"]], [a["path"], b["path"]]))
        record_source(self.root, work["batch_id"], a["source_id"], [rel])
        record_source(self.root, work["batch_id"], b["source_id"], [rel])
        commit(self.root, work["batch_id"])

        (self.root / "raw" / "sources" / "a.md").unlink()
        delete_work = prepare(self.root)
        assert delete_work is not None
        self._stage(
            delete_work,
            rel,
            page_text("Shared", [b["source_id"]], [b["path"]], "Only claims supported by B remain."),
        )
        result = commit(self.root, delete_work["batch_id"])
        self.assertEqual(result["pages_archived"], 0)
        active = (self.root / "wiki" / rel).read_text(encoding="utf-8")
        self.assertNotIn(a["source_id"], active)
        self.assertIn("supported by B", active)

    def test_move_preserves_source_identity(self) -> None:
        source_id, rel = self._ingest_one("old.md")
        old = self.root / "raw" / "sources" / "old.md"
        new = self.root / "raw" / "sources" / "folder" / "new.md"
        new.parent.mkdir(parents=True)
        old.rename(new)
        work = prepare(self.root)
        assert work is not None
        self.assertEqual(work["events"][0]["kind"], "moved")
        self.assertEqual(work["events"][0]["source_id"], source_id)
        rewrite_moved_paths(self.root, work["batch_id"])
        record_source(self.root, work["batch_id"], source_id, [rel])
        commit(self.root, work["batch_id"])
        manifest = load_json(paths_for(self.root).manifest, {})
        self.assertEqual(manifest["sources"][source_id]["path"], "raw/sources/folder/new.md")

    def test_concurrent_human_edit_refuses_commit(self) -> None:
        source_id, rel = self._ingest_one()
        self._source("alpha.md", "New source content.")
        work = prepare(self.root)
        assert work is not None
        self._stage(work, rel, page_text("Alpha", [source_id], ["raw/sources/alpha.md"], "AI version"))
        record_source(self.root, work["batch_id"], source_id, [rel])
        active = self.root / "wiki" / rel
        active.write_text(active.read_text(encoding="utf-8") + "\nHuman edit.\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "commit refused"):
            commit(self.root, work["batch_id"])
        self.assertIn("Human edit", active.read_text(encoding="utf-8"))

    def test_source_change_during_batch_refuses_stale_publish(self) -> None:
        source_id, rel = self._ingest_one()
        self._source("alpha.md", "Version two.")
        work = prepare(self.root)
        assert work is not None
        self._stage(work, rel, page_text("Alpha", [source_id], ["raw/sources/alpha.md"], "Version two."))
        record_source(self.root, work["batch_id"], source_id, [rel])
        self._source("alpha.md", "Version three arrived before commit.")
        with self.assertRaisesRegex(RuntimeError, "source changed during batch"):
            commit(self.root, work["batch_id"])

    def test_new_source_can_checkout_and_merge_existing_page(self) -> None:
        first_source_id, rel = self._ingest_one()
        self._source("beta.md", "Beta adds evidence to Alpha.")
        work = prepare(self.root)
        assert work is not None
        event = work["events"][0]
        checkout_page(self.root, work["batch_id"], rel)
        self._stage(
            work,
            rel,
            page_text(
                "Alpha",
                [first_source_id, event["source_id"]],
                ["raw/sources/alpha.md", "raw/sources/beta.md"],
                "Alpha now has two independent sources.",
            ),
        )
        record_source(self.root, work["batch_id"], event["source_id"], [rel])
        result = commit(self.root, work["batch_id"])
        self.assertEqual(result["pages_upserted"], 1)
        self.assertIn(event["source_id"], (self.root / "wiki" / rel).read_text(encoding="utf-8"))

    def test_same_path_different_bytes_gets_new_identity(self) -> None:
        old_id, rel = self._ingest_one()
        source = self.root / "raw" / "sources" / "alpha.md"
        source.unlink()
        delete_work = prepare(self.root)
        assert delete_work is not None
        (self.root / delete_work["staging_wiki"] / rel).unlink()
        commit(self.root, delete_work["batch_id"])
        source.write_text("A completely unrelated replacement.", encoding="utf-8")
        replacement = prepare(self.root)
        assert replacement is not None
        event = replacement["events"][0]
        self.assertEqual(event["kind"], "new")
        self.assertNotEqual(event["source_id"], old_id)

    def test_batch_delete_all_shared_sources_archives_once(self) -> None:
        self._source("a.md", "A")
        self._source("b.md", "B")
        work = prepare(self.root)
        assert work is not None
        events = work["events"]
        rel = "concepts/shared.md"
        self._stage(work, rel, page_text("Shared", [e["source_id"] for e in events], [e["path"] for e in events]))
        for event in events:
            record_source(self.root, work["batch_id"], event["source_id"], [rel])
        commit(self.root, work["batch_id"])
        for name in ("a.md", "b.md"):
            (self.root / "raw" / "sources" / name).unlink()
        delete_work = prepare(self.root)
        assert delete_work is not None
        (self.root / delete_work["staging_wiki"] / rel).unlink()
        result = commit(self.root, delete_work["batch_id"])
        self.assertEqual(result["pages_archived"], 1)
        restored = restore_trash(self.root, delete_work["batch_id"])
        self.assertEqual(restored["status"], "restored_for_review")
        self.assertTrue(Path(restored["path"]).joinpath(rel).exists())

    def test_concurrent_index_edit_refuses_commit(self) -> None:
        self._source("alpha.md", "Alpha")
        work = prepare(self.root)
        assert work is not None
        event = work["events"][0]
        rel = "concepts/alpha.md"
        self._stage(work, rel, page_text("Alpha", [event["source_id"]], [event["path"]]))
        record_source(self.root, work["batch_id"], event["source_id"], [rel])
        index = self.root / "wiki" / "index.md"
        index.write_text(index.read_text(encoding="utf-8") + "\nHuman index note.\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "system page changed"):
            commit(self.root, work["batch_id"])
        self.assertIn("Human index note", index.read_text(encoding="utf-8"))

    def test_mid_commit_error_rolls_back_active_files_and_manifest(self) -> None:
        source_id, rel = self._ingest_one()
        active = self.root / "wiki" / rel
        manifest_path = paths_for(self.root).manifest
        active_before = active.read_bytes()
        manifest_before = manifest_path.read_bytes()
        self._source("alpha.md", "Changed source.")
        work = prepare(self.root)
        assert work is not None
        self._stage(work, rel, page_text("Alpha", [source_id], ["raw/sources/alpha.md"], "Changed page."))
        record_source(self.root, work["batch_id"], source_id, [rel])
        with mock.patch("wiki_maintainer.core._append_log", side_effect=OSError("simulated disk failure")):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                commit(self.root, work["batch_id"])
        self.assertEqual(active.read_bytes(), active_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertFalse((self.root / ".wiki-trash" / work["batch_id"]).exists())
        self.assertEqual(status(self.root)["pending_batch"], work["batch_id"])

    def test_restore_searches_all_tombstones_at_same_path_by_hash(self) -> None:
        old_bytes = "Original A bytes."
        self._source("same.md", old_bytes)
        first = prepare(self.root)
        assert first is not None
        old_id = first["events"][0]["source_id"]
        record_source(self.root, first["batch_id"], old_id, [])
        commit(self.root, first["batch_id"])
        (self.root / "raw" / "sources" / "same.md").unlink()
        deletion = prepare(self.root)
        assert deletion is not None
        commit(self.root, deletion["batch_id"])

        self._source("same.md", "Replacement B bytes.")
        second = prepare(self.root)
        assert second is not None
        new_id = second["events"][0]["source_id"]
        record_source(self.root, second["batch_id"], new_id, [])
        commit(self.root, second["batch_id"])
        (self.root / "raw" / "sources" / "same.md").unlink()
        second_deletion = prepare(self.root)
        assert second_deletion is not None
        commit(self.root, second_deletion["batch_id"])

        self._source("same.md", old_bytes)
        restored = prepare(self.root)
        assert restored is not None
        self.assertEqual(restored["events"][0]["kind"], "restored")
        self.assertEqual(restored["events"][0]["source_id"], old_id)

    def test_complete_journal_with_stale_pending_finalizes_idempotently(self) -> None:
        self._source("alpha.md", "Alpha")
        work = prepare(self.root)
        assert work is not None
        event = work["events"][0]
        record_source(self.root, work["batch_id"], event["source_id"], [])
        expected = commit(self.root, work["batch_id"])
        paths = paths_for(self.root)
        work_path = paths.state / "batches" / work["batch_id"] / "work-order.json"
        atomic_json(paths.pending, {"batch_id": work["batch_id"], "work_order": str(work_path), "created_at": "crash-window"})
        replayed = commit(self.root, work["batch_id"])
        self.assertEqual(replayed, expected)
        self.assertIsNone(status(self.root)["pending_batch"])


if __name__ == "__main__":
    unittest.main()
