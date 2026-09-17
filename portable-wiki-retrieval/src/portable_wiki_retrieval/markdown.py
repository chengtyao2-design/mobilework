from __future__ import annotations

import fnmatch
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import RetrievalError, require


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#?]+\.md)(?:#[^)]+)?\)", re.IGNORECASE)
WORD_RE = re.compile(r"[a-z0-9_][a-z0-9_.-]*", re.IGNORECASE)
CJK_RE = re.compile(r"[\u3400-\u9fff]+")


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def search_tokens(value: str) -> list[str]:
    normalized = normalize_text(value)
    result = WORD_RE.findall(normalized)
    for segment in CJK_RE.findall(normalized):
        result.extend(segment)
        result.extend(segment[index:index + 2] for index in range(len(segment) - 1))
    return list(dict.fromkeys(token for token in result if token.strip()))


def document_id(wiki_id: str, relative_path: str) -> str:
    digest = hashlib.sha256(f"{wiki_id}:{relative_path}".encode("utf-8")).hexdigest()[:20]
    return f"doc_{digest}"


def chunk_id(doc_id: str, heading_path: str, ordinal: int) -> str:
    digest = hashlib.sha256(f"{doc_id}:{heading_path}:{ordinal}".encode("utf-8")).hexdigest()[:24]
    return f"chk_{digest}"


def _frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            raw = "".join(lines[1:index])
            try:
                value = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                return {}, content
            return (value if isinstance(value, dict) else {}), "".join(lines[index + 1:])
    return {}, content


def _as_aliases(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


@dataclass(slots=True)
class MarkdownDocument:
    wiki_id: str
    document_id: str
    root: Path
    path: Path
    relative_path: str
    title: str
    aliases: list[str]
    entity_type: str | None
    content: str
    sha256: str
    mtime_ns: int
    size: int
    links: list[str] = field(default_factory=list)


def parse_document(wiki_id: str, root: Path, path: Path) -> MarkdownDocument:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(resolved_root).as_posix()
    except ValueError as error:
        raise RetrievalError("PATH_OUTSIDE_ROOT", f"文件越过 Wiki 根目录：{path}") from error
    raw = resolved.read_text(encoding="utf-8", errors="replace")
    metadata, body = _frontmatter(raw)
    first_heading = HEADING_RE.search(body)
    title = str(metadata.get("title") or (first_heading.group(2).strip() if first_heading else resolved.stem))
    aliases = _as_aliases(metadata.get("aliases") or metadata.get("alias"))
    entity_type = metadata.get("entity_type") or metadata.get("type")
    links = WIKILINK_RE.findall(body) + MARKDOWN_LINK_RE.findall(body)
    stat = resolved.stat()
    return MarkdownDocument(
        wiki_id=wiki_id, document_id=document_id(wiki_id, relative), root=resolved_root,
        path=resolved, relative_path=relative, title=title, aliases=aliases,
        entity_type=str(entity_type) if entity_type is not None else None,
        content=body, sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        mtime_ns=stat.st_mtime_ns, size=stat.st_size, links=[item.strip() for item in links if item.strip()],
    )


def _matches(relative: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern)
        or Path(relative).match(pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatch(relative, pattern[3:]))
        for pattern in patterns
    )


def scan_documents(entry: dict[str, Any]) -> tuple[list[MarkdownDocument], dict[str, Any]]:
    started = time.perf_counter()
    root = Path(entry["root"]).resolve()
    require(root.is_dir(), "WIKI_ROOT_UNREADABLE", f"Wiki 目录不存在或不可读：{root}")
    includes = list(entry.get("include") or ["**/*.md"])
    excludes = list(entry.get("exclude") or [])
    documents: list[MarkdownDocument] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as error:
            raise RetrievalError("PATH_OUTSIDE_ROOT", f"文件越过 Wiki 根目录：{path}") from error
        if path.suffix.casefold() != ".md" or not _matches(relative, includes) or _matches(relative, excludes):
            continue
        document = parse_document(entry["wiki_id"], root, path)
        documents.append(document)
        total_bytes += document.size
    elapsed_ms = (time.perf_counter() - started) * 1000
    report = {
        "wiki_id": entry["wiki_id"], "root": str(root), "files": len(documents),
        "bytes": total_bytes, "scan_ms": elapsed_ms,
        "frontmatter_files": sum(1 for doc in documents if doc.aliases or doc.entity_type),
        "link_count": sum(len(doc.links) for doc in documents),
        "directories": sorted({str(Path(doc.relative_path).parent.as_posix()) for doc in documents}),
    }
    return documents, report


def chunk_document(document: MarkdownDocument, chunk_size: int, overlap: int,
                   include_heading: bool = True) -> list[dict[str, Any]]:
    require(chunk_size > 0 and 0 <= overlap < chunk_size, "CONFIG_OUT_OF_RANGE",
            "chunk overlap 必须非负且小于 chunk size")
    lines = document.content.splitlines()
    headings: list[str] = []
    sections: list[tuple[str, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            sections.append((" > ".join(headings), text))
        buffer.clear()

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            flush()
            level = len(match.group(1))
            headings[:] = headings[:level - 1]
            headings.append(match.group(2).strip())
        else:
            buffer.append(line)
    flush()
    if not sections and document.content.strip():
        sections = [(document.title, document.content.strip())]

    chunks: list[dict[str, Any]] = []
    ordinal = 0
    step = chunk_size - overlap
    for heading, text in sections:
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_size)
            if end < len(text):
                boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end), text.rfind(". ", start, end))
                if boundary > start + chunk_size // 2:
                    end = boundary + 1
            body = text[start:end].strip()
            if body:
                embedding_text = f"{document.title}\n{heading}\n{body}" if include_heading and heading else f"{document.title}\n{body}"
                chunks.append({
                    "chunk_id": chunk_id(document.document_id, heading, ordinal),
                    "document_id": document.document_id, "ordinal": ordinal,
                    "heading_path": heading, "text": body, "embedding_text": embedding_text,
                    "sha256": hashlib.sha256(embedding_text.encode("utf-8")).hexdigest(),
                })
                ordinal += 1
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
    return chunks
