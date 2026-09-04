import json
import os

from wiki_retrieval.embedding import load_dotenv


def test_registered_kb_loads_application_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    (tmp_path / ".env").write_text("EMBEDDING_API_KEY=test-app-value\n")
    (tmp_path / "mobilework.config.json").write_text(json.dumps({"knowledge_bases": {"a": {"path": "kb/a"}}}))
    kb = tmp_path / "kb/a"
    kb.mkdir(parents=True)
    load_dotenv(kb)
    assert os.environ["EMBEDDING_API_KEY"] == "test-app-value"


def test_unregistered_directory_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    (tmp_path / ".env").write_text("EMBEDDING_API_KEY=not-for-other-directory\n")
    (tmp_path / "mobilework.config.json").write_text(json.dumps({"knowledge_bases": {"a": {"path": "kb/a"}}}))
    kb = tmp_path / "unregistered"
    kb.mkdir()
    load_dotenv(kb)
    assert "EMBEDDING_API_KEY" not in os.environ


def test_process_environment_keeps_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "process-value")
    (tmp_path / ".env").write_text("EMBEDDING_API_KEY=file-value\n")
    load_dotenv(tmp_path)
    assert os.environ["EMBEDDING_API_KEY"] == "process-value"
