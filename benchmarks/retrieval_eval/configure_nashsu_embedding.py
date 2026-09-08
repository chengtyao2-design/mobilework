"""Temporarily configure and verify nashsu/llm_wiki for fair embedding evaluation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
from typing import Any
from urllib import request


ENDPOINT = "https://openrouter.ai/api/v1/embeddings"
MODEL = "qwen/qwen3-embedding-8b"
LOCAL_API_TOKEN = "retrieval-eval-local-token"


def app_state_path() -> Path:
    root = os.environ.get("APPDATA")
    if not root:
        raise RuntimeError("APPDATA is not set")
    return Path(root) / "com.llmwiki.app" / "app-state.json"


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def configure(project_root: Path, backup: Path) -> dict[str, Any]:
    key = (os.environ.get("EMBEDDING_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("EMBEDDING_API_KEY or OPENROUTER_API_KEY is required")
    project = read_json(project_root / ".llm-wiki" / "project.json")
    project_id = str(project.get("id") or "").strip()
    if not project_id:
        raise ValueError("nashsu evaluation project has no .llm-wiki/project.json id")
    state_path = app_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        if state_path.exists():
            shutil.copyfile(state_path, backup)
        else:
            backup.write_text("{}", encoding="utf-8")
    state = read_json(state_path)
    state["embeddingConfig"] = {
        "enabled": True,
        "endpoint": ENDPOINT,
        "apiKey": key,
        "model": MODEL,
        "maxChunkChars": 1500,
        "overlapChunkChars": 200,
        "concurrency": 1,
        "batchSize": 8,
        "extraHeaders": {},
    }
    state["apiConfig"] = {**dict(state.get("apiConfig") or {}), "enabled": True}
    registry = dict(state.get("projectRegistry") or {})
    registry[project_id] = {"name": project_root.name, "path": str(project_root.resolve())}
    state["projectRegistry"] = registry
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"state": str(state_path), "backup": str(backup), "project_id": project_id,
            "endpoint": ENDPOINT, "model": MODEL}


def restore(backup: Path) -> dict[str, Any]:
    if not backup.is_file():
        raise FileNotFoundError(backup)
    state_path = app_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(backup, state_path)
    return {"restored": str(state_path), "backup": str(backup)}


def post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = (os.environ.get("LLM_WIKI_API_TOKEN") or LOCAL_API_TOKEN).strip()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(base_url.rstrip("/") + path, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with request.urlopen(req, timeout=180) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict) or value.get("ok") is False:
        raise RuntimeError(str(value))
    return value


def embed_pages(base_url: str, project_id: str, project_root: Path, output: Path) -> dict[str, Any]:
    pages = sorted(path for path in (project_root / "wiki").rglob("*.md")
                   if path.name not in ("index.md", "log.md"))
    if len(pages) != 50:
        raise RuntimeError(f"expected 50 Wiki content pages, found {len(pages)}")
    previous = read_json(output).get("pages", []) if output.is_file() else []
    successful = {str(row.get("path")): row for row in previous
                  if isinstance(row, dict) and row.get("status") == "ok"}
    rows = []
    for page in pages:
        relative = page.relative_to(project_root).as_posix()
        if relative in successful:
            rows.append(successful[relative])
            continue
        try:
            response = post(base_url, f"/api/v1/projects/{project_id}/pages/embed",
                            {"path": relative, "force": True})
            rows.append({"path": relative, "status": "ok", "result": response.get("result")})
        except Exception as exc:
            rows.append({"path": relative, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"project_id": project_id, "model": MODEL, "pages": rows},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [row for row in rows if row["status"] != "ok"]
    if failed:
        raise RuntimeError(f"{len(failed)} of 50 nashsu pages failed embedding; see {output}")
    return {"project_id": project_id, "embedded_pages": len(rows), "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    configure_parser = sub.add_parser("configure")
    configure_parser.add_argument("--project-root", type=Path, required=True)
    configure_parser.add_argument("--backup", type=Path, required=True)
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--backup", type=Path, required=True)
    embed_parser = sub.add_parser("embed")
    embed_parser.add_argument("--base-url", default="http://127.0.0.1:19828")
    embed_parser.add_argument("--project-id", required=True)
    embed_parser.add_argument("--project-root", type=Path, required=True)
    embed_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "configure":
        result = configure(args.project_root.resolve(), args.backup.resolve())
    elif args.command == "restore":
        result = restore(args.backup.resolve())
    else:
        result = embed_pages(args.base_url, args.project_id, args.project_root.resolve(), args.output.resolve())
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
