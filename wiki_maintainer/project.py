from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MOBILEWORK_CONFIG = "mobilework.config.json"


class KnowledgeBaseError(ValueError):
    """Raised when a configured knowledge base cannot be resolved safely."""


def load_mobilework_config(app_root: Path) -> dict[str, Any]:
    path = app_root.resolve() / MOBILEWORK_CONFIG
    if not path.is_file():
        raise KnowledgeBaseError(f"missing {MOBILEWORK_CONFIG}: {path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise KnowledgeBaseError(f"invalid {MOBILEWORK_CONFIG}: {error}") from error
    knowledge_bases = config.get("knowledge_bases")
    if not isinstance(knowledge_bases, dict) or not knowledge_bases:
        raise KnowledgeBaseError(f"{MOBILEWORK_CONFIG} must define a non-empty knowledge_bases object")
    return config


def knowledge_base_entries(app_root: Path) -> dict[str, dict[str, Any]]:
    config = load_mobilework_config(app_root)
    entries: dict[str, dict[str, Any]] = {}
    for kb_id, raw in config["knowledge_bases"].items():
        if not isinstance(kb_id, str) or not kb_id.strip() or not isinstance(raw, dict):
            raise KnowledgeBaseError("knowledge base identifiers and definitions must be objects")
        relative = raw.get("path")
        if not isinstance(relative, str) or not relative.strip():
            raise KnowledgeBaseError(f"knowledge_bases.{kb_id}.path must be a non-empty string")
        resolved = (app_root.resolve() / relative).resolve()
        try:
            resolved.relative_to(app_root.resolve())
        except ValueError as error:
            raise KnowledgeBaseError(f"knowledge_bases.{kb_id}.path escapes the application root") from error
        entries[kb_id] = {**raw, "id": kb_id, "root": resolved}
    return entries


def resolve_kb_root(
    root: Path,
    *,
    kb: str | None = None,
    kb_root: Path | None = None,
) -> Path:
    """Resolve a KB root while retaining compatibility with legacy standalone projects."""
    if kb and kb_root is not None:
        raise KnowledgeBaseError("--kb and --kb-root are mutually exclusive")
    if kb_root is not None:
        resolved = kb_root.expanduser().resolve()
        if not (resolved / "wiki.config.json").is_file():
            raise KnowledgeBaseError(f"knowledge base has no wiki.config.json: {resolved}")
        return resolved

    app_root = root.expanduser().resolve()
    if (app_root / MOBILEWORK_CONFIG).is_file():
        config = load_mobilework_config(app_root)
        selected = kb or config.get("default_kb")
        if not isinstance(selected, str) or not selected:
            raise KnowledgeBaseError(f"{MOBILEWORK_CONFIG} must define default_kb when --kb is omitted")
        entries = knowledge_base_entries(app_root)
        if selected not in entries:
            choices = ", ".join(sorted(entries))
            raise KnowledgeBaseError(f"unknown knowledge base {selected!r}; configured: {choices}")
        resolved = entries[selected]["root"]
        if not (resolved / "wiki.config.json").is_file():
            raise KnowledgeBaseError(f"knowledge base {selected!r} has no wiki.config.json: {resolved}")
        return resolved

    if kb:
        raise KnowledgeBaseError(f"--kb requires {MOBILEWORK_CONFIG} at {app_root}")
    return app_root


def knowledge_base_id(app_root: Path, kb_root: Path) -> str | None:
    if not (app_root / MOBILEWORK_CONFIG).is_file():
        return None
    resolved = kb_root.resolve()
    return next(
        (kb_id for kb_id, entry in knowledge_base_entries(app_root).items() if entry["root"] == resolved),
        None,
    )
