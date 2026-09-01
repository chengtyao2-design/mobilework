from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wiki_maintainer.core import commit, initialize, load_json, paths_for, prepare, record_source
from wiki_maintainer.incremental import bm25_candidates, chunk_text, claim_marker, diff_chunks, parse_claims, prune_claims


def page(title: str, source_ids: list[str], sources: list[str], body: str) -> str:
    return "\n".join([
        "---", f"title: {json.dumps(title)}", 'category: "concepts"', 'tags: ["test"]',
        "aliases: []", "wiki_managed: true", f"source_ids: {json.dumps(source_ids)}",
        f"sources: {json.dumps(sources)}", f"summary: {json.dumps(title)}", "---", "",
        f"# {title}", "", body, "",
    ])


class IncrementalAlgorithmTests(unittest.TestCase):
    def test_chunk_diff_preserves_unchanged_chunks(self) -> None:
        old = chunk_text("Alpha one.\n\nBeta two.\n\nGamma three.", max_chars=20)
        new = chunk_text("Alpha one.\n\nBeta changed.\n\nGamma three.", max_chars=20)
        delta = diff_chunks(old, new)
        self.assertGreaterEqual(delta["unchanged_count"], 2)
        self.assertEqual(delta["added_count"], 1)
        self.assertEqual(delta["removed_count"], 1)

    def test_claim_prune_removes_exclusive_and_keeps_shared(self) -> None:
        text = "\n\n".join([
            "A-only statement.\n" + claim_marker("clm_a", ["src_a"], ["chk_a"]),
            "Shared statement.\n" + claim_marker("clm_ab", ["src_a", "src_b"], ["chk_b"]),
        ])
        updated, result = prune_claims(text, {"src_a"})
        self.assertNotIn("A-only statement", updated)
        self.assertIn("Shared statement", updated)
        self.assertEqual(parse_claims(updated)[0].source_ids, ("src_b",))
        self.assertEqual(result["removed_claims"], ["clm_a"])

    def test_identity_candidates_rank_exact_alias_then_lexical(self) -> None:
        documents = [
            {"page": "concepts/transformer.md", "title": "Transformer Architecture", "aliases": ["Transformer"], "text": "Transformer attention architecture"},
            {"page": "concepts/cnn.md", "title": "Convolutional Network", "aliases": [], "text": "image convolution network"},
        ]
        self.assertTrue(bm25_candidates("Transformer", documents)[0]["exact_identity"])
        self.assertEqual(bm25_candidates("attention architecture", documents)[0]["page"], "concepts/transformer.md")


class IncrementalIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        initialize(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _source(self, name: str, text: str) -> None:
        target = self.root / "raw" / "sources" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _stage(self, work: dict, rel: str, text: str) -> None:
        target = self.root / work["staging_wiki"] / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_manifest_records_chunks_claims_and_modified_diff(self) -> None:
        self._source("a.md", "First paragraph.\n\nSecond paragraph.")
        work = prepare(self.root)
        assert work
        event = work["events"][0]
        chunk_id = event["chunks"][0]["chunk_id"]
        rel = "concepts/a.md"
        body = "A supported claim.\n" + claim_marker("clm_a", [event["source_id"]], [chunk_id])
        self._stage(work, rel, page("A", [event["source_id"]], [event["path"]], body))
        record_source(self.root, work["batch_id"], event["source_id"], [rel])
        commit(self.root, work["batch_id"])
        manifest = load_json(paths_for(self.root).manifest, {})
        self.assertEqual(manifest["version"], 3)
        self.assertEqual(manifest["pages"][rel]["claims"][0]["id"], "clm_a")

        self._source("a.md", "First paragraph changed.\n\nSecond paragraph.")
        modified = prepare(self.root)
        assert modified
        delta = modified["events"][0]["diff"]
        self.assertGreater(delta["added_count"], 0)
        self.assertGreater(delta["removed_count"], 0)

    def test_exact_duplicate_identity_is_rejected(self) -> None:
        self._source("a.md", "A")
        first = prepare(self.root)
        assert first
        event = first["events"][0]
        self._stage(first, "concepts/a.md", page("Same", [event["source_id"]], [event["path"]], "A"))
        record_source(self.root, first["batch_id"], event["source_id"], ["concepts/a.md"])
        commit(self.root, first["batch_id"])

        self._source("b.md", "B")
        second = prepare(self.root)
        assert second
        event = second["events"][0]
        self._stage(second, "concepts/b.md", page("Same", [event["source_id"]], [event["path"]], "B"))
        record_source(self.root, second["batch_id"], event["source_id"], ["concepts/b.md"])
        with self.assertRaisesRegex(ValueError, "duplicate page identity"):
            commit(self.root, second["batch_id"])


if __name__ == "__main__":
    unittest.main()
