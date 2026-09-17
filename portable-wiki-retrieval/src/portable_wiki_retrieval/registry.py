from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .errors import RetrievalError, require


WIKI_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DEFAULT_EXCLUDES = [
    ".git/**", "**/.git/**",
    ".portable-wiki/**", "**/.portable-wiki/**",
    ".wiki-state/**", "**/.wiki-state/**",
]


class WikiRegistry:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "registry.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RetrievalError("CONFIG_INVALID", f"Wiki 注册表损坏：{error}") from error
        entries = body.get("wikis", {}) if isinstance(body, dict) else {}
        require(isinstance(entries, dict), "CONFIG_INVALID", "Wiki 注册表格式不正确")
        return entries

    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = {"version": 1, "wikis": entries}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def list(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        values = list(self._load().values())
        if enabled_only:
            values = [entry for entry in values if entry.get("enabled", True)]
        return sorted(values, key=lambda item: item["wiki_id"])

    def get(self, wiki_id: str, *, require_enabled: bool = False) -> dict[str, Any]:
        entry = self._load().get(wiki_id)
        if entry is None or (require_enabled and not entry.get("enabled", True)):
            raise RetrievalError("WIKI_NOT_FOUND", f"Wiki 未注册或已禁用：{wiki_id}")
        return entry

    def register(
        self,
        wiki_id: str,
        root: str | Path,
        *,
        name: str | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        require(bool(WIKI_ID_RE.fullmatch(wiki_id)), "INVALID_ARGUMENT",
                "wiki_id 必须由小写字母、数字和单个连字符组成")
        resolved = Path(root).expanduser().resolve()
        require(resolved.is_dir(), "WIKI_ROOT_UNREADABLE", f"Wiki 目录不存在或不可读：{resolved}")
        entries = self._load()
        require(wiki_id not in entries, "INVALID_ARGUMENT", f"wiki_id 已存在：{wiki_id}")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry = {
            "wiki_id": wiki_id,
            "name": name or wiki_id,
            "root": str(resolved),
            "enabled": bool(enabled),
            "include": include or ["**/*.md"],
            "exclude": exclude or list(DEFAULT_EXCLUDES),
            "created_at": now,
            "updated_at": now,
        }
        entries[wiki_id] = entry
        self._save(entries)
        return entry

    def update(self, wiki_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"name", "root", "enabled", "include", "exclude", "settings"}
        unknown = set(changes) - allowed
        require(not unknown, "INVALID_ARGUMENT", f"不支持的 Wiki 设置：{sorted(unknown)}")
        entries = self._load()
        require(wiki_id in entries, "WIKI_NOT_FOUND", f"Wiki 未注册：{wiki_id}")
        entry = dict(entries[wiki_id])
        if "root" in changes:
            resolved = Path(changes["root"]).expanduser().resolve()
            require(resolved.is_dir(), "WIKI_ROOT_UNREADABLE", f"Wiki 目录不存在或不可读：{resolved}")
            changes["root"] = str(resolved)
        entry.update({key: value for key, value in changes.items() if value is not None})
        entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entries[wiki_id] = entry
        self._save(entries)
        return entry

    def unregister(self, wiki_id: str, *, remove_index: bool = True) -> dict[str, Any]:
        entries = self._load()
        entry = entries.pop(wiki_id, None)
        require(entry is not None, "WIKI_NOT_FOUND", f"Wiki 未注册：{wiki_id}")
        self._save(entries)
        index_path = self.state_dir / "indexes" / wiki_id
        if remove_index and index_path.is_dir():
            shutil.rmtree(index_path)
        return {"wiki_id": wiki_id, "unregistered": True, "index_removed": remove_index,
                "wiki_files_deleted": False}
