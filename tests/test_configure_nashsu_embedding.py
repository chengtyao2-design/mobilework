from __future__ import annotations

import json
from pathlib import Path

from benchmarks.retrieval_eval.configure_nashsu_embedding import ENDPOINT, MODEL, configure, restore


def test_configure_and_restore_nashsu_embedding_state(tmp_path: Path, monkeypatch):
    appdata = tmp_path / "appdata"
    state = appdata / "com.llmwiki.app" / "app-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"updateCheckState": {"done": True}}), encoding="utf-8")
    project = tmp_path / "nashsu"
    (project / ".llm-wiki").mkdir(parents=True)
    (project / ".llm-wiki" / "project.json").write_text(
        json.dumps({"id": "retrieval-eval"}), encoding="utf-8")
    backup = tmp_path / "backup.json"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")

    result = configure(project, backup)
    configured = json.loads(state.read_text(encoding="utf-8"))
    assert result["project_id"] == "retrieval-eval"
    assert configured["embeddingConfig"]["endpoint"] == ENDPOINT
    assert configured["embeddingConfig"]["model"] == MODEL
    assert configured["projectRegistry"]["retrieval-eval"]["path"] == str(project.resolve())

    restore(backup)
    assert json.loads(state.read_text(encoding="utf-8")) == {"updateCheckState": {"done": True}}
