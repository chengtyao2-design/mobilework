# MCP 服务：注册 retrieve / graph_neighbors / knowledge_tree 三个工具；向上查找定位项目根；启动校验 wiki 与 raw/sources 存在
"""stdio MCP server exposing retrieve / graph_neighbors / knowledge_tree.

stdout belongs to JSON-RPC. Logging goes to stderr and every import or scan that
might print is wrapped in redirect_stdout, because one stray byte on stdout makes
the handshake fail with nothing but "server exited".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
for _noisy in ("httpx", "httpcore", "lancedb", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("wiki_retrieval")


def configure_observability(root: Path) -> Path:
    """Persist compact retrieval lifecycle logs without query or evidence text."""
    level_name = (os.environ.get("MOBILEWORK_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    target = root / ".mobilework-state" / "logs" / "retrieval.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(level)
    if not any(getattr(handler, "_mobilework_observability", False) for handler in logger.handlers):
        handler = RotatingFileHandler(
            target, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        handler._mobilework_observability = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return target

SERVER_NAME = "mobile-retrieval"
ROOT_CATEGORY = "(root)"


def resolve_root() -> Path:
    from . import loader

    root = loader.find_root()
    for candidate in (root, *root.parents):
        if (candidate / "mobilework.config.json").is_file():
            return candidate
    for required in (loader.WIKI_DIR, loader.SOURCE_DIR):
        if not (root / required).is_dir():
            raise FileNotFoundError(
                f"required retrieval directory does not exist: {root / required}"
            )
    return root


def _category(path: str, prefix: str) -> str:
    relative = path[len(prefix) :] if path.startswith(prefix) else path
    head, separator, _ = relative.partition("/")
    return head if separator else ROOT_CATEGORY


def catalog(root: Path) -> dict:
    """Deterministic catalogue: totals plus the page list of every wiki category.
    Chunk ids come from the vector table when one exists."""
    from . import index, loader, store
    from .textutil import frontmatter_value

    corpus = loader.get_corpus(root)
    chunks_by_page: dict[str, list[str]] = {}
    for chunk in store.list_chunks(corpus.root):
        chunks_by_page.setdefault(chunk["page_id"], []).append(chunk["chunk_id"])
    for ids in chunks_by_page.values():
        ids[:] = sorted(set(ids))

    categories: dict[str, list[dict]] = {}
    for doc in corpus.wiki_docs:
        page = {
            "page_id": doc.page_id,
            "path": doc.path,
            "title": doc.title,
        }
        page_type = frontmatter_value(doc.content, "type")
        if page_type is not None:
            page["page_type"] = page_type
        chunk_ids = chunks_by_page.get(doc.page_id)
        if chunk_ids:
            page["chunk_ids"] = chunk_ids
        categories.setdefault(_category(doc.path, f"{loader.WIKI_DIR}/"), []).append(page)
    for pages in categories.values():
        pages.sort(key=lambda page: page["path"])

    return {
        "knowledge_base": corpus.root.name,
        "chunking_strategy": index.CHUNKING_STRATEGY,
        "totals": {
            "wiki_pages": len(corpus.wiki_docs),
            "source_files": len(corpus.source_docs),
            "categories": len(categories),
            "chunks": sum(len(ids) for ids in chunks_by_page.values()),
        },
        "index": index.freshness(corpus.root),
        "wiki": [
            {"category": name, "pages": categories[name]} for name in sorted(categories)
        ],
        "sources": [
            {"source_id": doc.page_id, "path": doc.path, "title": doc.title}
            for doc in corpus.source_docs
        ],
    }


def neighbors(root: Path, seed: str, depth: int = 1, top_k: int = 10) -> dict:
    from . import loader
    from .channels import graph

    return graph.neighbors(loader.get_corpus(root), seed, depth, top_k)


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _retrieval_dump(payload: dict, max_bytes: int = 14000) -> str:
    """Compact MCP evidence; full diagnostics remain in the Python API.

    Remove lowest-ranked evidence before the client's 16 KB truncation can
    cut JSON mid-sentence. Keep complete chunks and explicitly disclose omission.
    """
    result = {key: payload[key] for key in (
        "query", "scope", "routing", "queried_kb_ids", "errors",
        "partial_failure", "duplicate", "timings", "embedding", "channels") if key in payload}
    result["kb_status"] = {
        kb: {key: value for key, value in status.items()
             if key in ("channels", "embedding", "duplicate")}
        for kb, status in payload.get("kb_status", {}).items()
    }
    result["results"] = []
    result["omitted_results"] = 0
    encode = lambda: json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    for hit in payload.get("results", []):
        page = {key: hit[key] for key in (
            "page_id", "path", "title", "scope", "kb_id", "kb_name",
            "rank_in_kb", "global_score", "matched_claims", "verification_status",
            "freshness_factor", "source_titles", "source_metadata") if key in hit}
        metadata = hit.get("source_metadata", {})
        citation = {
            "knowledge_base": hit.get("kb_name", hit.get("kb_id", "")),
            "title": hit.get("title", ""),
            "evidence_type": "original_source" if hit.get("scope") == "source" else "wiki_summary",
        }
        if hit.get("source_titles"):
            citation["derived_from"] = hit["source_titles"]
        for key in ("publisher", "source_published_at", "source_updated_at", "effective_from", "effective_to", "source_url"):
            if metadata.get(key):
                citation[key] = metadata[key]
        page["citation"] = citation
        page["matched_chunks"] = [
            {key: value for key, value in chunk.items()
             if key in ("chunk_id", "heading_path", "text", "claim_ids")}
            for chunk in hit.get("matched_chunks", [])
        ]
        result["results"].append(page)
        if len(encode().encode("utf-8")) > max_bytes:
            result["results"].pop()
            result["omitted_results"] += 1
    return encode()


def _surface_errors(function):
    """The SDK forwards ToolError text to the model but hides other exceptions as
    a server crash, so anything the caller could fix must be raised as ToolError."""
    from mcp.server.mcpserver.exceptions import ToolError

    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, FileNotFoundError) as error:
            raise ToolError(str(error)) from error
        except RuntimeError as error:  # CorpusEmptyError and embedding key errors
            raise ToolError(str(error)) from error

    return wrapper


def build_server(root: Path):
    from mcp.server.mcpserver import MCPServer

    from .retrieve import DEFAULT_TOP_K, retrieve as _retrieve
    from .fusion import DEFAULT_RRF_K

    server = MCPServer(name=SERVER_NAME)

    @server.tool(
        description=(
            "Search the knowledge base with RRF fusion over the vector, keyword and "
            "graph channels, followed by one-hop graph expansion. scope='wiki' reads "
            "the constructed pages, scope='source' reads raw/sources (graph channel "
            "off), scope='both' reads everything. An embedding failure degrades that "
            "one channel and is reported under 'embedding'. No match is an empty "
            "'results' list, not an error. include_content=true adds only bounded "
            "matched_chunks[].text evidence, never complete page bodies."
        )
    )
    @_surface_errors
    def retrieve(
        query: str,
        scope: Literal["wiki", "source", "both"] = "wiki",
        channels: list[str] | None = None,
        top_k: int = DEFAULT_TOP_K,
        rrf_k: float = DEFAULT_RRF_K,
        include_content: bool = False,
        verbose: bool = False,
        kb_ids: list[str] | None = None,
        profile: str | None = None,
        overrides: dict | None = None,
    ) -> str:
        started = time.perf_counter()
        logger.info(
            "event=tool_started tool=retrieve profile=%s scope=%s kb_count=%d query_chars=%d",
            profile or "default", scope, len(kb_ids or []), len(query),
        )
        try:
            payload = _retrieve(
                query,
                scope=scope,
                channels=channels,
                top_k=top_k,
                rrf_k=rrf_k,
                include_content=include_content,
                verbose=verbose,
                root=root,
                kb_ids=kb_ids,
                profile=profile,
                overrides=overrides,
            )
        except Exception as error:
            logger.error(
                "event=tool_failed tool=retrieve elapsed_ms=%.1f error_type=%s error=%s",
                (time.perf_counter() - started) * 1000.0,
                type(error).__name__,
                str(error).replace("\n", " ")[:300],
            )
            raise
        logger.info(
            "event=tool_completed tool=retrieve elapsed_ms=%.1f result_count=%d partial_failure=%s",
            (time.perf_counter() - started) * 1000.0,
            len(payload.get("results", [])),
            payload.get("partial_failure", False),
        )
        return _retrieval_dump(payload)

    @server.tool(
        description=(
            "Walk the wikilink graph out from one page. 'seed' accepts a page id, a "
            "path or a title; neighbours are returned with their hop distance."
        )
    )
    @_surface_errors
    def graph_neighbors(seed: str, depth: int = 1, top_k: int = 10, kb_id: str | None = None) -> str:
        from .federated import registry
        bases = registry(root)
        if kb_id is None and len(bases) != 1:
            raise ValueError("kb_id is required for multi-KB graph traversal")
        identifier = kb_id or next(iter(bases))
        if identifier not in bases:
            raise ValueError("unknown kb_id")
        result = neighbors(bases[identifier]["root"], seed.removeprefix(identifier + "::"), depth, top_k)
        result["kb_id"] = identifier
        return _dump(result)

    @server.tool(
        description=(
            "Return the deterministic catalogue of the knowledge base: totals, wiki "
            "pages grouped by category with their vector chunk ids, raw source ids, "
            "and the freshness of the vector index."
        )
    )
    @_surface_errors
    def knowledge_tree(kb_ids: list[str] | None = None) -> str:
        from .federated import registry
        bases = registry(root)
        if not (root / "mobilework.config.json").is_file() and kb_ids is None:
            return _dump(catalog(root))
        identifiers = list(bases) if kb_ids is None else kb_ids
        if not identifiers or set(identifiers) - bases.keys():
            raise ValueError("unknown kb_ids")
        return _dump({"knowledge_bases": [{"kb_id": i, **catalog(bases[i]["root"])} for i in identifiers]})

    @server.tool()
    @_surface_errors
    def list_knowledge_bases() -> str:
        from .federated import list_knowledge_bases as listing
        return _dump(listing(root))

    @server.tool()
    @_surface_errors
    def route_knowledge_bases(query: str, kb_ids: list[str] | None = None, top_k: int = 3) -> str:
        from .federated import route_knowledge_bases as routing
        return _dump(routing(root, query, kb_ids, top_k))

    return server


def main() -> None:
    # Corpus loading and the lancedb/pyarrow imports run with stdout diverted, so
    # anything they print cannot corrupt the JSON-RPC stream.
    with contextlib.redirect_stdout(sys.stderr):
        from . import embedding, loader

        root = resolve_root()
        embedding.load_dotenv(root)
        log_path = configure_observability(root)
        from .federated import registry
        corpus = loader.get_corpus(next(iter(registry(root).values()))["root"])
        server = build_server(root)
        logger.info(
            "event=server_started server=%s root=%s wiki_pages=%d source_files=%d log=%s",
            SERVER_NAME,
            root,
            len(corpus.wiki_docs),
            len(corpus.source_docs),
            log_path,
        )
    server.run("stdio")


if __name__ == "__main__":
    main()
