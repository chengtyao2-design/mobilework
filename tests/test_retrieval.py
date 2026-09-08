"""Contract tests for the retrieval side.

Nothing here touches the network or builds a LanceDB table: with no table on
disk `store.search` returns [], which is exactly the offline keyword+graph path.
The vector channel is exercised by patching `embedding.fetch` and `store.search`
with constructed hits.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from wiki_retrieval import chunking, dedup, index, loader, server, store
from wiki_retrieval import embedding as embedding_module
from wiki_retrieval.channels import graph, keyword
from wiki_retrieval.fusion import Run, rrf
from wiki_retrieval.retrieve import retrieve
from wiki_retrieval.textutil import snippet, tokenize_query

ALPHA = """---
title: "阿尔法概念"
category: "concepts"
type: "concept"
---

# UNIQUEHEADING

阿尔法是一个用于测试的概念，它引用了 [[entities/beta|贝塔]] 作为关联实体。
这里有一个 zzneedle 词用于精确匹配测试。
"""

BETA = """---
title: "贝塔实体"
category: "entities"
---

# 贝塔实体

贝塔是另一个页面，不含前一个页面的独有词。
"""

GAMMA = """---
title: "伽玛实体"
---

# 伽玛实体

伽玛只能被向量通道命中，它链接到 [[entities/delta|德尔塔]]。
"""

DELTA = """---
title: "德尔塔实体"
---

# 德尔塔实体

德尔塔只能通过图扩展被带出来。
"""

SOURCE = """# 原始资料甲

这份原始资料包含 zzneedle 以及 rawonly 两个标记词。
"""


def hit(page_id: str, score: float) -> dict:
    return {
        "chunk_id": f"{page_id}#0",
        "page_id": page_id,
        "chunk_index": 0,
        "heading_path": f"# {page_id}",
        "chunk_text": f"vector evidence for {page_id}",
        "score": score,
    }


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch: pytest.MonkeyPatch):
    # Retrieval tests are offline by contract. Keep credentials configured on the
    # developer machine from turning a unit test into a live network request.
    for name in embedding_module._KEY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    loader.reset_cache()
    dedup.reset()
    yield
    loader.reset_cache()
    dedup.reset()


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "entities").mkdir(parents=True)
    (root / "raw" / "sources").mkdir(parents=True)
    (root / "wiki" / "concepts" / "alpha.md").write_text(ALPHA, encoding="utf-8")
    (root / "wiki" / "entities" / "beta.md").write_text(BETA, encoding="utf-8")
    (root / "wiki" / "entities" / "gamma.md").write_text(GAMMA, encoding="utf-8")
    (root / "wiki" / "entities" / "delta.md").write_text(DELTA, encoding="utf-8")
    (root / "raw" / "sources" / "src-alpha.md").write_text(SOURCE, encoding="utf-8")
    return root


@pytest.fixture
def corpus(corpus_root: Path):
    return loader.get_corpus(corpus_root)


@pytest.fixture
def live_vector():
    """Pretends a key is configured so the vector channel runs; the fake embedding
    never leaves the process."""
    with mock.patch.object(embedding_module, "has_api_key", return_value=True):
        with mock.patch.object(
            embedding_module, "fetch", return_value=([0.1, 0.2], 1.0)
        ):
            yield


class TestTextUtil:
    def test_tokenize_cjk_emits_bigrams_and_chars(self):
        tokens = tokenize_query("the 注意力机制")
        assert "the" not in tokens
        assert "注意" in tokens
        assert "注" in tokens
        assert "注意力机制" in tokens
        assert tokens == sorted(set(tokens))

    def test_tokenize_drops_single_chars_and_stopwords(self):
        assert tokenize_query("a 的") == []

    def test_snippet_drops_heading_lines(self):
        text = snippet(ALPHA, "zzneedle")
        assert "UNIQUEHEADING" not in text
        assert "zzneedle" in text
        assert len(text) <= 80 + 160


class TestChunking:
    def test_heading_aware_chunks_are_bounded_and_drop_frontmatter(self):
        content = "---\ntitle: Example\nsecret: metadata\n---\n# Example\n\n"
        content += "## First\n\n" + ("甲。" * 650)
        content += "\n\n## Second\n\n" + ("乙。" * 650)
        chunks = chunking.chunk_document(
            page_id="example", title="Example", content=content, scope="wiki"
        )

        assert len(chunks) >= 2
        assert all(len(item["text"]) <= chunking.WIKI_MAX_CHARS for item in chunks)
        assert all("secret: metadata" not in item["text"] for item in chunks)
        assert any("Second" in item["heading_path"] for item in chunks)
        assert all(item["embedding_text"].startswith("Example") for item in chunks)

    def test_claim_markers_are_not_indexed_or_returned(self):
        chunks = chunking.chunk_document(
            page_id="claims",
            title="Claims",
            content='# Claims\n\nEvidence.\n<!-- wiki-claim: {"id":"internal"} -->',
            scope="wiki",
        )
        assert "wiki-claim" not in chunks[0]["text"]
        assert "internal" not in chunks[0]["embedding_text"]

    def test_chunk_ids_are_stable(self):
        arguments = {
            "page_id": "stable",
            "title": "Stable",
            "content": "# Stable\n\n## A\n\n" + ("内容。" * 600),
            "scope": "wiki",
        }
        first = chunking.chunk_document(**arguments)
        second = chunking.chunk_document(**arguments)
        assert [item["chunk_id"] for item in first] == [item["chunk_id"] for item in second]


class TestLoader:
    def test_scopes_and_totals(self, corpus):
        assert corpus.totals() == {"wiki": 4, "source": 1}
        assert not corpus.is_empty
        assert [doc.page_id for doc in corpus.docs("source")] == ["src-alpha"]
        assert len(corpus.docs("both")) == 5
        with pytest.raises(ValueError):
            corpus.docs("nonsense")

    def test_duplicate_page_ids_are_rejected(self, corpus_root: Path):
        (corpus_root / "wiki" / "entities" / "alpha.md").write_text("# dup", encoding="utf-8")
        loader.reset_cache()
        with pytest.raises(loader.DuplicatePageIdError) as caught:
            loader.get_corpus(corpus_root)
        message = str(caught.value)
        assert "duplicate page_id 'alpha'" in message
        assert "wiki/concepts/alpha.md" in message
        assert "wiki/entities/alpha.md" in message

    def test_empty_corpus_is_an_error_not_an_empty_result(self, tmp_path: Path):
        root = tmp_path / "bare"
        (root / "wiki").mkdir(parents=True)
        (root / "raw" / "sources").mkdir(parents=True)
        with pytest.raises(loader.CorpusEmptyError):
            retrieve("zzneedle", root=root)

    def test_corpus_is_cached_until_a_file_changes(self, corpus_root: Path):
        first = loader.get_corpus(corpus_root)
        assert loader.get_corpus(corpus_root) is first
        target = corpus_root / "wiki" / "entities" / "beta.md"
        os.utime(target, (first.max_mtime + 100, first.max_mtime + 100))
        assert loader.get_corpus(corpus_root) is not first


class TestKeywordChannel:
    def test_exact_page_id_match_wins(self, corpus_root: Path):
        payload = retrieve("alpha", top_k=5, root=corpus_root)
        top = payload["results"][0]
        assert top["page_id"] == "alpha"
        assert top["raw_scores"]["keyword"] >= 200.0

    def test_no_match_is_an_empty_result(self, corpus_root: Path):
        payload = retrieve("zzabsentzz", top_k=5, root=corpus_root)
        assert payload["results"] == []
        assert payload["corpus"] == {"wiki": 4, "source": 1}

    def test_keyword_only_when_no_key_is_configured(self, corpus_root: Path):
        # top_k=1 makes the graph quota 0, so no expansion masks the mode.
        payload = retrieve("zzneedle", top_k=1, root=corpus_root)
        assert payload["embedding"]["status"] == "disabled"
        assert payload["graph_expansion"] == {"slots": 0, "used": 0}
        assert payload["results"][0]["page_id"] == "alpha"
        assert set(payload["results"][0]["ranks"]) == {"keyword", "graph"}

    def test_include_content(self, corpus_root: Path):
        payload = retrieve("alpha", top_k=2, include_content=True, root=corpus_root)
        result = payload["results"][0]
        assert "content" not in result
        assert result["matched_chunks"][0]["text"]
        without_content = retrieve("alpha", top_k=2, root=corpus_root)["results"][0]
        assert "text" not in without_content["matched_chunks"][0]

    def test_evidence_is_limited_per_page_and_globally(self, corpus_root: Path):
        for number in range(6):
            (corpus_root / "wiki" / "concepts" / f"budget-{number}.md").write_text(
                f"# Budget {number}\n\n## Evidence\n\n" + ("budgetneedle。" * 260),
                encoding="utf-8",
            )
        loader.reset_cache()
        payload = retrieve(
            "budgetneedle",
            channels=["keyword"],
            top_k=6,
            include_content=True,
            root=corpus_root,
        )
        assert all(len(result["matched_chunks"]) <= 2 for result in payload["results"])
        texts = [
            chunk["text"]
            for result in payload["results"]
            for chunk in result["matched_chunks"]
            if "text" in chunk
        ]
        assert texts
        assert sum(map(len, texts)) <= 8000


class TestFusion:
    def test_rrf_fusion_scores(self, corpus_root: Path, live_vector):
        with mock.patch.object(store, "search", return_value=[hit("beta", 0.9)]):
            payload = retrieve(
                "zzneedle", channels=["keyword", "vector"], top_k=5, root=corpus_root
            )
        assert payload["embedding"]["status"] == "ok"
        by_id = {r["page_id"]: r for r in payload["results"]}
        # alpha is rank 1 on keyword only; beta is rank 1 on vector only.
        assert by_id["alpha"]["fused_score"] == pytest.approx(1.0 / 61.0, abs=1e-12)
        assert by_id["beta"]["fused_score"] == pytest.approx(1.0 / 61.0, abs=1e-12)
        assert by_id["beta"]["raw_scores"]["vector"] == 0.9
        assert by_id["beta"]["matched_chunks"][0]["chunk_id"] == "beta#0"
        assert by_id["alpha"]["ranks"] == {"keyword": 1}

    def test_runs_keep_untruncated_order_for_ablation(self, corpus_root: Path):
        payload = retrieve("实体", top_k=1, verbose=True, root=corpus_root)
        assert len(payload["results"]) == 1
        assert len(payload["runs"]["keyword"]["ordered"]) > 1
        assert "timings" in payload

    def test_timings_and_runs_are_gated_by_verbose(self, corpus_root: Path):
        payload = retrieve("实体", top_k=1, root=corpus_root)
        assert "runs" not in payload
        assert "timings" not in payload

    def test_rrf_k_is_validated(self):
        with pytest.raises(ValueError):
            rrf([Run(name="keyword")], rrf_k=0.5)
        with pytest.raises(ValueError):
            rrf([Run(name="keyword")], rrf_k=10_001)

    def test_weighted_rrf_preserves_a_strong_semantic_hit(self):
        vector_run = Run(name="vector", ordered=["gold"], entries={"gold": {"path": "gold"}})
        keyword_run = Run(name="keyword", ordered=["noise", "gold"], entries={
            "noise": {"path": "noise"}, "gold": {"path": "gold"}})
        ranking = rrf([vector_run, keyword_run], weights={"vector": 1.0, "keyword": 0.35})["ranking"]
        assert [row["path"] for row in ranking] == ["gold", "noise"]


class TestGraphChannel:
    def test_graph_run_scores_matched_above_neighbours(self, corpus):
        run = graph.run(corpus, "zzneedle")
        by_path = {path: run.scores[path] for path in run.ordered}
        alpha = "wiki/concepts/alpha.md"
        beta = "wiki/entities/beta.md"
        assert by_path[alpha] >= graph.MATCHED_ENTITY_SCORE
        assert graph.NEIGHBOUR_SCORE <= by_path[beta] < graph.MATCHED_ENTITY_SCORE
        assert run.entries[beta]["graph_related_to"] == ["阿尔法概念"]
        assert "wiki/entities/gamma.md" not in run.ordered

    def test_quota_is_zero_for_a_single_result(self):
        assert graph.quota(1, 0) == 0
        assert graph.quota(5, 0) == 2
        assert graph.quota(5, 5) == 1

    def test_graph_expansion_is_quota_bound(self, corpus_root: Path, live_vector):
        # Only the vector channel can see gamma, so its neighbour delta can only
        # arrive through the expansion step.
        with mock.patch.object(store, "search", return_value=[hit("gamma", 0.9)]):
            payload = retrieve(
                "qqabsentqq",
                channels=["vector", "graph"],
                top_k=5,
                include_content=True,
                root=corpus_root,
            )
        assert payload["graph_expansion"]["slots"] == 2
        assert payload["graph_expansion"]["used"] == 1
        assert len(payload["results"]) <= 5
        supplements = [r for r in payload["results"] if r["origin"] == "graph_expand"]
        assert [r["page_id"] for r in supplements] == ["delta"]
        assert supplements[0]["snippet"].startswith("Graph neighbor of ")
        assert supplements[0]["graph_related_to"] == ["伽玛实体"]
        assert supplements[0]["fused_score"] == pytest.approx(1.0 / 61.0, abs=1e-12)
        assert "content" not in supplements[0]
        assert supplements[0]["matched_chunks"] == []

    def test_neighbors_walks_by_distance(self, corpus_root: Path):
        payload = server.neighbors(corpus_root, "alpha", depth=1, top_k=10)
        assert payload["resolved_path"] == "wiki/concepts/alpha.md"
        assert [n["page_id"] for n in payload["neighbors"]] == ["beta"]
        assert payload["neighbors"][0]["distance"] == 1

    def test_neighbors_accepts_a_title_and_rejects_an_unknown_seed(self, corpus_root: Path):
        assert server.neighbors(corpus_root, "贝塔实体")["neighbors"][0]["page_id"] == "alpha"
        with pytest.raises(ValueError):
            server.neighbors(corpus_root, "no-such-page")


class TestScopeAndValidation:
    def test_source_scope_drops_the_graph_channel(self, corpus_root: Path):
        payload = retrieve("zzneedle", scope="source", top_k=5, root=corpus_root)
        assert payload["channels"] == ["vector", "keyword"]
        assert "graph_expansion" not in payload
        assert [r["page_id"] for r in payload["results"]] == ["src-alpha"]
        assert payload["results"][0]["scope"] == "source"

    def test_source_scope_ignores_wiki_only_text(self, corpus_root: Path):
        assert retrieve("UNIQUEHEADING", scope="source", root=corpus_root)["results"] == []

    def test_both_scope_mixes_wiki_and_source(self, corpus_root: Path):
        payload = retrieve("zzneedle", scope="both", top_k=5, root=corpus_root)
        assert {r["scope"] for r in payload["results"]} == {"wiki", "source"}

    def test_invalid_arguments_raise(self, corpus_root: Path):
        with pytest.raises(ValueError):
            retrieve("   ", root=corpus_root)
        with pytest.raises(ValueError):
            retrieve("alpha", scope="everything", root=corpus_root)
        with pytest.raises(ValueError):
            retrieve("alpha", channels=["semantic"], root=corpus_root)
        with pytest.raises(ValueError):
            retrieve("alpha", channels=["graph"], scope="source", root=corpus_root)
        with pytest.raises(ValueError):
            retrieve("alpha", rrf_k=0.5, root=corpus_root)


class TestEmbeddingDegradation:
    def test_failure_degrades_but_still_answers(self, corpus_root: Path):
        boom = RuntimeError("endpoint exploded")
        with mock.patch.object(embedding_module, "has_api_key", return_value=True):
            with mock.patch.object(embedding_module, "fetch", side_effect=boom):
                payload = retrieve("zzneedle", top_k=5, root=corpus_root)
        assert payload["embedding"]["status"] == "degraded"
        assert "endpoint exploded" in payload["embedding"]["error"]
        assert payload["results"]
        assert "vector" not in payload["results"][0]["ranks"]

    def test_missing_key_disables_the_channel_without_calling_out(self, corpus_root: Path):
        with mock.patch.object(embedding_module, "fetch") as fetch:
            payload = retrieve("zzneedle", top_k=5, root=corpus_root)
        fetch.assert_not_called()
        assert payload["embedding"]["status"] == "disabled"

    def test_redact_hides_a_live_key(self):
        with mock.patch.dict(os.environ, {"EMBEDDING_API_KEY": "sk-secret-value"}):
            redacted = embedding_module.redact("failed with sk-secret-value")
        assert "sk-secret-value" not in redacted
        assert "[redacted]" in redacted

    def test_endpoint_and_model_fall_back_when_unset(self):
        with mock.patch.dict(os.environ, {"EMBEDDING_BASE_URL": "", "EMBEDDING_MODEL": ""}):
            assert embedding_module.endpoint() == "https://openrouter.ai/api/v1/embeddings"
            assert embedding_module.model_name() == embedding_module.DEFAULT_MODEL
        with mock.patch.dict(
            os.environ, {"EMBEDDING_BASE_URL": "https://example.test/v1/"}
        ):
            assert embedding_module.endpoint() == "https://example.test/v1/embeddings"

    def test_legacy_key_name_still_works(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_KEY": "sk-legacy"}, clear=True):
            assert embedding_module.has_api_key()
            assert embedding_module._api_key() == "sk-legacy"


class TestDedup:
    def test_repeat_call_is_flagged(self, corpus_root: Path):
        first = retrieve("zzneedle", top_k=5, root=corpus_root)
        second = retrieve("  ZZneedle ", top_k=5, root=corpus_root)
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["previous"]["top_page_ids"] == [
            r["page_id"] for r in first["results"][:5]
        ]
        assert second["previous"]["calls"] == 1

    def test_different_scope_is_not_a_duplicate(self, corpus_root: Path):
        retrieve("zzneedle", top_k=5, root=corpus_root)
        assert retrieve("zzneedle", scope="source", top_k=5, root=corpus_root)["duplicate"] is False

    def test_switch_off_disables_detection(self, corpus_root: Path):
        with mock.patch.dict(os.environ, {"WIKI_RETRIEVAL_DEDUP": "off"}):
            retrieve("zzneedle", top_k=5, root=corpus_root)
            assert retrieve("zzneedle", top_k=5, root=corpus_root)["duplicate"] is False

    def test_cache_is_bounded(self):
        for n in range(dedup.CAPACITY + 10):
            dedup.remember("wiki", ["keyword"], f"query {n}", {"result_count": 0})
        assert len(dedup._seen) == dedup.CAPACITY


class TestIndexFreshness:
    def test_missing_index_reads_as_stale(self, corpus_root: Path):
        state = index.freshness(corpus_root)
        assert state == {
            "exists": False,
            "stale_index": True,
            "reason": "no vector index has been built",
            "corpus_max_mtime": state["corpus_max_mtime"],
        }
        assert state["corpus_max_mtime"] > 0

    def test_old_chunking_strategy_is_stale(self, corpus_root: Path):
        meta = index.write_meta(corpus_root, dim=4, rows=4)
        meta["chunking_strategy"] = "whole_page_v1"
        index.meta_path(corpus_root).write_text(json.dumps(meta), encoding="utf-8")
        state = index.freshness(corpus_root)
        assert state["stale_index"] is True
        assert "chunking strategy changed" in state["reason"]

    def test_index_rows_cover_content_after_eight_thousand_chars(self, corpus_root: Path):
        target = corpus_root / "wiki" / "concepts" / "long.md"
        target.write_text("# Long\n\n## Start\n\n" + ("前。" * 4200) + "\n\n## Tail\n\nTAIL_SENTINEL", encoding="utf-8")
        rows = index._rows(corpus_root, None)
        assert any("TAIL_SENTINEL" in row["chunk_text"] for row in rows)
        assert len([row for row in rows if row["page_id"] == "long"]) > 1

    def test_build_embeds_every_chunk_and_records_page_count(self, corpus_root: Path):
        target = corpus_root / "wiki" / "concepts" / "long.md"
        target.write_text("# Long\n\n## A\n\n" + ("内容。" * 1800), encoding="utf-8")
        captured = {}

        def fake_fetch_batch(texts):
            captured.setdefault("batches", []).append(list(texts))
            return [[0.1, 0.2] for _ in texts], 1.0

        def fake_replace(root, rows, dim):
            captured["rows"] = list(rows)
            captured["dim"] = dim

        with mock.patch.object(embedding_module, "has_api_key", return_value=True), mock.patch.object(
            embedding_module, "fetch_batch", side_effect=fake_fetch_batch
        ), mock.patch.object(store, "replace_all", side_effect=fake_replace):
            meta = index.build(corpus_root, progress=lambda _: None)

        embedded = [text for batch in captured["batches"] for text in batch]
        assert len(captured["rows"]) == len(embedded) == meta["rows"]
        assert meta["rows"] > meta["wiki_pages"]
        assert meta["chunking_strategy"] == index.CHUNKING_STRATEGY
        assert captured["dim"] == 2

    def test_meta_then_edit_flips_stale_index(self, corpus_root: Path):
        meta = index.write_meta(corpus_root, dim=4, rows=4)
        fresh = index.freshness(corpus_root)
        assert fresh["stale_index"] is False
        assert fresh["dim"] == 4 and fresh["rows"] == 4
        assert fresh["index_built_at"] == meta["index_built_at"]

        target = corpus_root / "wiki" / "concepts" / "alpha.md"
        newer = meta["corpus_max_mtime"] + 100
        os.utime(target, (newer, newer))
        assert index.freshness(corpus_root)["stale_index"] is True

    def test_freshness_is_reported_inside_retrieve(self, corpus_root: Path):
        payload = retrieve("alpha", root=corpus_root)
        assert payload["index"]["stale_index"] is True

    def test_search_without_a_table_is_empty_not_an_error(self, corpus_root: Path):
        assert store.search(corpus_root, [0.1, 0.2], 5) == []
        assert store.list_chunks(corpus_root) == []


class TestKnowledgeTree:
    def test_tree_is_deterministic(self, corpus_root: Path):
        first = server.catalog(corpus_root)
        loader.reset_cache()
        assert server.catalog(corpus_root) == first
        assert first["chunking_strategy"] == index.CHUNKING_STRATEGY
        assert first["totals"]["wiki_pages"] == 4
        assert first["totals"]["source_files"] == 1
        assert [group["category"] for group in first["wiki"]] == ["concepts", "entities"]
        assert first["sources"][0]["source_id"] == "src-alpha"

    def test_page_type_comes_from_frontmatter(self, corpus_root: Path):
        tree = server.catalog(corpus_root)
        concepts = next(g for g in tree["wiki"] if g["category"] == "concepts")
        assert concepts["pages"][0]["page_type"] == "concept"


class TestServerSurface:
    def test_validation_errors_surface_as_tool_errors(self):
        # A bare exception reaches the model as an opaque crash, so the decorator
        # must convert it into a ToolError carrying the message.
        from mcp.server.mcpserver.exceptions import ToolError

        @server._surface_errors
        def rejects():
            raise ValueError("rrf_k must be between 1 and 10000")

        @server._surface_errors
        def empty():
            raise loader.CorpusEmptyError("no documents")

        @server._surface_errors
        def succeeds():
            return "payload"

        with pytest.raises(ToolError, match="rrf_k must be between"):
            rejects()
        with pytest.raises(ToolError, match="no documents"):
            empty()
        assert succeeds() == "payload"

    def test_resolve_root_requires_both_corpus_directories(self, tmp_path: Path):
        bare = tmp_path / "bare"
        (bare / "wiki").mkdir(parents=True)
        with mock.patch.dict(os.environ, {loader.PROJECT_ENV: str(bare)}):
            with pytest.raises(FileNotFoundError):
                server.resolve_root()

    def test_three_tools_are_registered(self, corpus_root: Path):
        built = server.build_server(corpus_root)
        names = {tool.name for tool in built._tool_manager.list_tools()}
        assert names == {"retrieve", "graph_neighbors", "knowledge_tree", "list_knowledge_bases", "route_knowledge_bases"}


class TestRealCorpus:
    def test_source_scope_answers_from_the_checked_in_sources(self, project_root: Path):
        payload = retrieve("离职", scope="source", top_k=5, root=project_root)
        assert payload["corpus"]["source"] > 0
        assert payload["results"]
        assert all(r["scope"] == "source" for r in payload["results"])

    def test_source_content_is_returned_only_as_bounded_chunks(self, project_root: Path):
        payload = retrieve("离职", scope="source", top_k=5, include_content=True, root=project_root)
        assert payload["results"]
        assert all("content" not in result for result in payload["results"])
        evidence = [
            chunk["text"]
            for result in payload["results"]
            for chunk in result["matched_chunks"]
            if "text" in chunk
        ]
        assert evidence
        assert sum(map(len, evidence)) <= 8000
