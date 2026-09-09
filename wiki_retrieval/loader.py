# 语料加载：读取 wiki/ 与 raw/sources/，校验 page_id 全局唯一，区分「库为空」与「无命中」
"""Corpus loading shared by every channel.

The corpus is loaded once per process and reused until a file mtime changes, so
a long-lived MCP server does not re-scan on every call. Two failure modes are
deliberately distinct: an empty corpus is an error the caller must fix, while a
query that simply matches nothing is an ordinary empty result.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .chunking import chunk_document
from .textutil import extract_links, extract_title, page_id as stem_of, snippet

MAX_CORPUS_FILES = 10_000
WIKI_DIR = "wiki"
SOURCE_DIR = "raw/sources"
# 导航/审计产物，非知识页面，不进入检索语料
WIKI_EXCLUDE = frozenset({"wiki/index.md", "wiki/log.md"})
_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030")


class CorpusEmptyError(RuntimeError):
    """Raised when neither wiki/ nor raw/sources/ holds a readable document."""


class DuplicatePageIdError(ValueError):
    """Raised at load time: page ids key the vector index, so a collision would
    silently drop a page instead of failing loudly."""


@dataclass(slots=True)
class Doc:
    path: str
    page_id: str
    title: str
    content: str
    links: list[str]
    scope: str
    lower_content: str = ""
    lower_title_text: str = ""
    chunks: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.lower_content = self.content.lower()
        self.lower_title_text = f"{self.title} {self.page_id}.md".lower()
        self.chunks = chunk_document(
            page_id=self.page_id,
            title=self.title,
            content=self.content,
            scope=self.scope,
        )


@dataclass(slots=True)
class Corpus:
    root: Path
    wiki_docs: list[Doc] = field(default_factory=list)
    source_docs: list[Doc] = field(default_factory=list)
    adjacency: dict[str, set[str]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    built_ms: float = 0.0
    max_mtime: float = 0.0

    def docs(self, scope: str) -> list[Doc]:
        if scope == "wiki":
            return self.wiki_docs
        if scope == "source":
            return self.source_docs
        if scope == "both":
            return self.wiki_docs + self.source_docs
        raise ValueError(f"scope must be 'wiki', 'source' or 'both', got '{scope}'")

    def totals(self) -> dict:
        return {"wiki": len(self.wiki_docs), "source": len(self.source_docs)}

    @property
    def is_empty(self) -> bool:
        return not self.wiki_docs and not self.source_docs

    def by_path(self, scope: str = "both") -> dict[str, Doc]:
        return {doc.path: doc for doc in self.docs(scope)}


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def validate_unique_page_ids(entries: list[tuple[str, str]], kind: str = "page_id") -> None:
    """`entries` are (identifier, path) pairs; the first conflict is reported with
    both paths so the caller can rename one of them."""
    seen: dict[str, str] = {}
    for identifier, path in entries:
        previous = seen.get(identifier)
        if previous is not None:
            raise DuplicatePageIdError(
                f"duplicate {kind} '{identifier}': '{previous}' and '{path}'"
            )
        seen[identifier] = path


def _load_scope(root: Path, relative_dir: str, scope: str, md_only: bool) -> list[Doc]:
    directory = root / relative_dir
    if not directory.is_dir():
        return []
    docs: list[Doc] = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        if len(docs) >= MAX_CORPUS_FILES:
            break
        if path.name.startswith("."):
            continue
        if md_only and path.suffix.lower() != ".md":
            continue
        relative = path.relative_to(root).as_posix()
        if scope == "wiki" and relative in WIKI_EXCLUDE:
            continue
        content = _read_text(path)
        if content is None:
            continue
        docs.append(
            Doc(
                path=relative,
                page_id=stem_of(relative),
                title=extract_title(content, path),
                content=content,
                links=extract_links(content) if scope == "wiki" else [],
                scope=scope,
            )
        )
    docs.sort(key=lambda doc: doc.path)
    validate_unique_page_ids(
        [(doc.page_id, doc.path) for doc in docs],
        "page_id" if scope == "wiki" else "source_id",
    )
    return docs


PROJECT_ENV = "WIKI_RETRIEVAL_PROJECT"


def find_root(start: Path | None = None) -> Path:
    """WIKI_RETRIEVAL_PROJECT wins; otherwise walk up looking for the corpus, so
    the server works when launched from a subdirectory."""
    configured = (os.environ.get(PROJECT_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    current = Path(start or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / WIKI_DIR).is_dir() and (candidate / SOURCE_DIR).is_dir():
            return candidate
    return current


def scan_max_mtime(root: Path, scope: str = "both") -> float:
    """Cheap stat-only scan. Must stay side-effect free: `index.freshness` calls it
    from WP-A's offline `doctor` path."""
    directories = []
    if scope in ("wiki", "both"):
        directories.append(Path(root) / WIKI_DIR)
    if scope in ("source", "both"):
        directories.append(Path(root) / SOURCE_DIR)
    newest = 0.0
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            try:
                if not path.is_file():
                    continue
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return newest


def load_corpus(root: Path) -> Corpus:
    from .channels.graph import build_adjacency

    started = time.perf_counter()
    root = Path(root)
    wiki_docs = _load_scope(root, WIKI_DIR, "wiki", md_only=True)
    source_docs = _load_scope(root, SOURCE_DIR, "source", md_only=False)
    adjacency, aliases = build_adjacency(wiki_docs)
    return Corpus(
        root=root,
        wiki_docs=wiki_docs,
        source_docs=source_docs,
        adjacency=adjacency,
        aliases=aliases,
        built_ms=(time.perf_counter() - started) * 1000.0,
        max_mtime=scan_max_mtime(root),
    )


_CACHE: dict[str, Corpus] = {}


def get_corpus(root: Path) -> Corpus:
    """Reloads only when a file under wiki/ or raw/sources/ has been touched."""
    resolved = Path(root).expanduser().resolve()
    key = str(resolved)
    cached = _CACHE.get(key)
    if cached is not None and cached.max_mtime == scan_max_mtime(resolved):
        return cached
    corpus = load_corpus(resolved)
    _CACHE[key] = corpus
    return corpus


def reset_cache() -> None:
    _CACHE.clear()


def result_stub(doc: Doc, include_content: bool = False, anchor: str = "") -> dict:
    stub = {
        "page_id": doc.page_id,
        "path": doc.path,
        "title": doc.title,
        "scope": doc.scope,
        "fused_score": 0.0,
        "ranks": {},
        "raw_scores": {},
        "contributions": {},
        "snippet": snippet(doc.content, anchor) if anchor else "",
        "matched_chunks": [],
        "graph_related_to": [],
    }
    if doc.scope == "source":
        stub["source_metadata"] = source_metadata(doc.content)
    else:
        from .textutil import frontmatter_value
        raw_sources = frontmatter_value(doc.content, "sources")
        if raw_sources:
            try:
                sources = json.loads(raw_sources)
            except (TypeError, ValueError):
                sources = [raw_sources]
            if isinstance(sources, list):
                stub["source_titles"] = [
                    Path(str(source)).stem for source in sources if str(source).strip()
                ]
    return stub


def source_metadata(content: str) -> dict:
    """Expose source dates without substituting filesystem or ingest dates."""
    from .textutil import frontmatter_value
    values = {}
    for key in ("source_url", "publisher", "published_at", "source_published_at",
                "source_updated_at", "updated_at", "effective_from", "effective_to",
                "accessed_at", "access_status", "license", "extraction_type"):
        value = frontmatter_value(content, key)
        if value and value.lower() not in ("null", "none", "~"):
            values[key] = value
    if "source_published_at" not in values and "published_at" in values:
        values["source_published_at"] = values["published_at"]
    if "source_updated_at" not in values and "updated_at" in values:
        values["source_updated_at"] = values["updated_at"]
    return values


def chunk_result(chunk: dict, include_content: bool, **scores: float) -> dict:
    result = {
        "chunk_id": chunk["chunk_id"],
        "page_id": chunk["page_id"],
        "chunk_index": int(chunk["chunk_index"]),
        "heading_path": chunk["heading_path"],
        "scores": {name: float(value) for name, value in scores.items()},
        "claim_ids": list(chunk.get("claim_ids", [])),
    }
    if include_content:
        result["text"] = chunk["text"]
    return result
