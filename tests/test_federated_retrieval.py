import json
import logging
import threading
from unittest.mock import patch

import pytest

from wiki_retrieval import federated, index, loader
from wiki_retrieval.claims import enrich, temporal_intent
from wiki_retrieval.chunking import chunk_document
from wiki_retrieval.retrieve import retrieve


@pytest.fixture
def project(tmp_path):
    config = {"knowledge_bases": {}}
    for name, topic in (("a", "benefits retention"), ("b", "ocean fisheries")):
        root = tmp_path / "kb" / name
        (root / "wiki").mkdir(parents=True)
        (root / "raw/sources").mkdir(parents=True)
        (root / "wiki/index.md").write_text(topic, encoding="utf-8")
        (root / "wiki/shared.md").write_text("# Shared\n\n" + topic + " common", encoding="utf-8")
        (root / "raw/sources/raw.md").write_text(topic, encoding="utf-8")
        config["knowledge_bases"][name] = {"path": "kb/" + name, "priority": 1}
    (tmp_path / "mobilework.config.json").write_text(json.dumps(config), encoding="utf-8")
    loader.reset_cache()
    return tmp_path


def test_route_and_no_match_fallback(project):
    assert federated.route_knowledge_bases(project, "benefits retention")["selected_kb_ids"] == ["a"]
    assert federated.route_knowledge_bases(project, "unknown")["selected_kb_ids"] == ["a", "b"]


def test_conversational_query_expansion_is_visible_and_improves_routing(project):
    root = project / "kb" / "a"
    (root / "wiki/index.md").write_text("古树 名木 树龄 保护等级", encoding="utf-8")
    routed = federated.route_knowledge_bases(project, "那些老树是按多少岁分档的？")
    assert routed["selected_kb_ids"] == ["a"]
    assert routed["query_expansion"] == {
        "applied": True,
        "terms": ["古树", "名木", "树龄", "分级", "保护等级"],
    }


def test_namespace_collision_and_hard_boundary(project):
    result = retrieve("common", root=project, channels=["keyword"], include_content=True)
    assert {r["page_id"] for r in result["results"]} == {"a::shared", "b::shared"}
    assert len({r["matched_chunks"][0]["chunk_id"] for r in result["results"]}) == 2
    result = retrieve("common", root=project, kb_ids=["b"], channels=["keyword"])
    assert {r["kb_id"] for r in result["results"]} == {"b"}
    with pytest.raises(ValueError):
        retrieve("common", root=project, kb_ids=["missing"])


def test_rrf_weights_and_stability(project):
    first = federated.retrieve(project, "common", channels=["keyword"], channel_weights={"keyword": 2})
    assert first["results"][0]["global_score"] == pytest.approx(2 / 61)
    assert [r["kb_id"] for r in first["results"]] == ["a", "b"]


def test_global_rrf_uses_local_fused_rank_not_channel_rank(project):
    from importlib import import_module
    module = import_module("wiki_retrieval.retrieve")
    fake = {"results": [{"page_id": "x", "path": "wiki/x.md", "ranks": {"vector": 7, "keyword": 2}, "matched_chunks": []},
                        {"page_id": "y", "path": "wiki/y.md", "ranks": {"vector": 1}, "matched_chunks": []}]}
    with patch.object(module, "retrieve", return_value=fake):
        result = federated.retrieve(project, "common", kb_ids=["a"], channel_weights={"vector": 2, "keyword": 3})
    assert result["results"][0]["global_score"] == pytest.approx(5 / 61)
    assert result["results"][1]["global_score"] == pytest.approx(2 / 62)
    assert result["results"][0]["ranks"] == {"vector": 7, "keyword": 2}


def test_fast_profile_passes_vector_only(project):
    with patch.object(federated, "retrieve", return_value={}) as delegate:
        retrieve("common", root=project, profile="fast")
    assert delegate.call_args.kwargs["channels"] == ["vector"]


def test_raw_source_dates_are_exposed(project):
    raw = project / "kb/a/raw/sources/raw.md"
    raw.write_text("---\npublished_at: 2024-01\nsource_updated_at: 2024-06\nsource_url: https://example.org/paper\neffective_from: null\n---\n\n# Benefits\n\nbenefits retention", encoding="utf-8")
    result = retrieve("benefits", root=project, kb_ids=["a"], scope="source", channels=["keyword"])
    metadata = result["results"][0]["source_metadata"]
    assert metadata["source_published_at"] == "2024-01"
    assert metadata["source_updated_at"] == "2024-06"
    assert "effective_from" not in metadata
    state = project / "kb/a/.wiki-state"
    state.mkdir()
    manifest = {"sources": {"s1": {"path": "raw/sources/raw.md"}},
                "pages": {"x.md": {"claims": [{"id": "clm_x", "source_ids": ["s1"]}]}}}
    (state / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    entry = {"path": "wiki/x.md", "matched_chunks": [{"claim_ids": ["clm_x"]}], "global_score": 1, "fused_score": 1}
    enrich(entry, project / "kb/a", "current benefits", "both")
    claim = entry["matched_claims"][0]
    assert claim["source_published_at"] == "2024-01"
    assert claim["source_updated_at"] == "2024-06"
    assert claim["source_metadata"][0]["source_url"] == "https://example.org/paper"


def test_partial_failure_and_fallback(project):
    import wiki_retrieval.retrieve as unused
    from importlib import import_module
    module = import_module("wiki_retrieval.retrieve")
    original = module.retrieve
    def local(query, root, **kwargs):
        if root.name == "a":
            raise RuntimeError("unavailable")
        return original(query, root=root, **kwargs)
    with patch.object(module, "retrieve", side_effect=local):
        result = federated.retrieve(project, "common", channels=["keyword"])
    assert result["partial_failure"]
    assert result["errors"] == {"a": "unavailable"}
    assert result["results"][0]["kb_id"] == "b"
    (project / "kb/a/wiki/shared.md").write_text("# Nothing\n\nnone", encoding="utf-8")
    result = federated.retrieve(project, "benefits retention", channels=["keyword"])
    assert result["queried_kb_ids"] == ["a", "b"]


def test_source_index_scope(project):
    rows = index._rows(project / "kb/a", None)
    assert {r["scope"] for r in rows} == {"wiki", "source"}


def test_kb_queries_run_concurrently(project):
    from importlib import import_module
    module = import_module("wiki_retrieval.retrieve")
    barrier = threading.Barrier(2, timeout=3)
    def local(query, root, **kwargs):
        barrier.wait()
        return {"results": [], "duplicate": False}
    with patch.object(module, "retrieve", side_effect=local):
        result = federated.retrieve(project, "common", channels=["keyword"])
    assert result["errors"] == {}
    assert result["queried_kb_ids"] == ["a", "b"]


def test_federated_retrieval_emits_compact_lifecycle_logs(project, caplog):
    with caplog.at_level(logging.INFO, logger="wiki_retrieval.federated"):
        result = federated.retrieve(project, "common", channels=["keyword"])
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=retrieval_started" in messages
    assert "event=retrieval_routed" in messages
    assert messages.count("event=kb_started") == 2
    assert messages.count("event=kb_completed") == 2
    assert "event=retrieval_completed" in messages
    assert f"result_count={len(result['results'])}" in messages
    assert "common" not in messages


def test_claim_many_to_many_and_context(project):
    content = '# Test\n\nFirst assertion.\n<!-- wiki-claim: {"id":"clm_a","source_ids":["s1","s2"],"chunk_ids":["c1","c2"]} -->\n\nSecond assertion.\n<!-- wiki-claim: {"id":"clm_b","source_ids":["s1"],"chunk_ids":["c1"]} -->'
    chunks = chunk_document(page_id="test", title="Test", content=content, scope="wiki")
    assert chunks[0]["claim_ids"] == ["clm_a", "clm_b"]
    assert "wiki-claim" not in chunks[0]["text"]
    root = project / "kb/a"
    (root / ".wiki-state").mkdir()
    (root / ".wiki-state/manifest.json").write_text(json.dumps({"pages": {"test.md": {"claims": [{"id": "clm_a", "source_ids": ["s1", "s2"], "chunk_ids": ["c1", "c2"], "source_updated_at": "2025-01-01", "verification_status": "verified"}]}}}), encoding="utf-8")
    entry = {"path": "wiki/test.md", "matched_chunks": chunks, "global_score": 1, "fused_score": 1}
    enrich(entry, root, "latest rules", "both")
    assert .9 <= entry["fused_score"] <= 1.1
    assert entry["matched_claims"][0]["source_count"] == 2
    entry["fused_score"] = 1
    enrich(entry, root, "explain benefits", "rerank")
    assert entry["fused_score"] == 1
    assert temporal_intent("现行规则")


def test_registry_rejects_path_escape(project):
    (project / "mobilework.config.json").write_text(json.dumps({"knowledge_bases": {"bad": {"path": "../outside"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="inside"):
        federated.registry(project)
