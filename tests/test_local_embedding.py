from pathlib import Path
from unittest import mock

from wiki_retrieval import embedding, index, local_embedding


def _fake_model(root: Path) -> Path:
    (root / "onnx").mkdir(parents=True)
    (root / "onnx/model_quantized.onnx").write_bytes(b"model")
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    return root


def test_local_backend_is_available_without_api_key(tmp_path, monkeypatch):
    model = _fake_model(tmp_path / "model")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDING_LOCAL_MODEL_PATH", str(model))
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    assert embedding.available()
    assert embedding.model_name() == "local/BAAI/bge-small-zh-v1.5-quantized"


def test_local_fetch_distinguishes_documents_and_queries(tmp_path, monkeypatch):
    model = _fake_model(tmp_path / "model")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDING_LOCAL_MODEL_PATH", str(model))
    with mock.patch.object(local_embedding, "encode", return_value=[[1.0, 0.0]]) as encode:
        vectors, _ = embedding.fetch_batch(["document"])
        assert vectors == [[1.0, 0.0]]
        encode.assert_called_once_with(["document"], query=False)
    with mock.patch.object(local_embedding, "encode", return_value=[[0.0, 1.0]]) as encode:
        vector, _ = embedding.fetch("query")
        assert vector == [0.0, 1.0]
        encode.assert_called_once_with(["query"], query=True)


def test_embedding_model_change_marks_index_stale(tmp_path, monkeypatch):
    corpus_root = tmp_path / "kb"
    page = corpus_root / "wiki/concepts/page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n\ncontent", encoding="utf-8")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openrouter")
    monkeypatch.setenv("EMBEDDING_MODEL", "model-a")
    index.write_meta(corpus_root, dim=4, rows=1)
    monkeypatch.setenv("EMBEDDING_MODEL", "model-b")
    state = index.freshness(corpus_root)
    assert state["stale_index"] is True
    assert "embedding model changed" in state["reason"]
