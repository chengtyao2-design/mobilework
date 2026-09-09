import json

from wiki_retrieval.server import _retrieval_dump


def test_mcp_evidence_is_valid_bounded_json_with_provenance():
    payload = {"results": [{"page_id": f"kb::{i}", "kb_id": "kb", "kb_name": "企业知识库",
                           "title": "海尔", "scope": "source", "raw_scores": {"vector": 1},
                           "source_metadata": {"publisher": "示例出版社", "source_published_at": "2025", "source_url": "https://example.org/source"},
                           "matched_chunks": [{"chunk_id": str(i), "text": "业务目标" * 400}]}
                          for i in range(10)],
               "errors": {"other": "unavailable"}, "partial_failure": True}
    encoded = _retrieval_dump(payload)
    result = json.loads(encoded)
    assert len(encoded.encode("utf-8")) <= 14000
    assert result["results"][0]["page_id"] == "kb::0"
    assert result["results"][0]["matched_chunks"][0]["text"] == "业务目标" * 400
    assert result["results"][0]["citation"] == {
        "knowledge_base": "企业知识库", "title": "海尔", "evidence_type": "original_source",
        "publisher": "示例出版社", "source_published_at": "2025", "source_url": "https://example.org/source",
    }
    assert result["omitted_results"] > 0
    assert result["partial_failure"] and result["errors"]
    assert "raw_scores" not in result["results"][0]
    assert len(payload["results"]) == 10
