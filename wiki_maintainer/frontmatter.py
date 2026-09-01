from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManagedPage:
    path: Path
    title: str
    category: str
    summary: str
    source_ids: tuple[str, ...]
    sources: tuple[str, ...]
    aliases: tuple[str, ...]
    managed: bool


def _frontmatter(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    return match.group(1) if match else None


def _scalar(block: str, key: str, default: str = "") -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*?)\s*$", block, re.MULTILINE)
    if not match:
        return default
    raw = match.group(1).strip()
    try:
        parsed = json.loads(raw)
        return str(parsed) if not isinstance(parsed, (list, dict)) else default
    except json.JSONDecodeError:
        return raw.strip("'\"")


def _list(block: str, key: str) -> tuple[str, ...]:
    inline = re.search(rf"^{re.escape(key)}:\s*(\[.*?\])\s*$", block, re.MULTILINE)
    if inline:
        try:
            value = json.loads(inline.group(1))
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return tuple(value)
        except json.JSONDecodeError:
            pass

    lines = block.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            values: list[str] = []
            for candidate in lines[index + 1 :]:
                item = re.match(r"^\s+-\s+(.*?)\s*$", candidate)
                if not item:
                    break
                raw = item.group(1)
                try:
                    values.append(str(json.loads(raw)))
                except json.JSONDecodeError:
                    values.append(raw.strip("'\""))
            return tuple(values)
    return ()


def parse_managed_page(path: Path) -> ManagedPage:
    text = path.read_text(encoding="utf-8")
    block = _frontmatter(text)
    if block is None:
        raise ValueError(f"missing YAML frontmatter: {path}")
    managed = _scalar(block, "wiki_managed", "false").lower() == "true"
    return ManagedPage(
        path=path,
        title=_scalar(block, "title", path.stem),
        category=_scalar(block, "category", path.parent.name),
        summary=_scalar(block, "summary"),
        source_ids=_list(block, "source_ids"),
        sources=_list(block, "sources"),
        aliases=_list(block, "aliases"),
        managed=managed,
    )


def replace_source_path(text: str, old_path: str, new_path: str) -> str:
    """Replace an exact JSON/YAML scalar path, never a loose substring."""
    quoted_old = json.dumps(old_path, ensure_ascii=False)
    quoted_new = json.dumps(new_path, ensure_ascii=False)
    return text.replace(quoted_old, quoted_new)


def replace_list_field(text: str, key: str, values: list[str]) -> str:
    """Replace a required JSON-style frontmatter list without touching the body."""
    block = _frontmatter(text)
    if block is None:
        raise ValueError("missing YAML frontmatter")
    replacement = f"{key}: {json.dumps(values, ensure_ascii=False)}"
    pattern = rf"^{re.escape(key)}:\s*\[.*?\]\s*$"
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"frontmatter field must use an inline JSON list: {key}")
    return updated
