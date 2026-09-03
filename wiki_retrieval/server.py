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
import sys
from functools import wraps
from pathlib import Path
from typing import Literal

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
for _noisy in ("httpx", "httpcore", "lancedb", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("wiki_retrieval")

SERVER_NAME = "mobile-retrieval"
ROOT_CATEGORY = "(root)"


def resolve_root() -> Path:
    from . import loader

    root = loader.find_root()
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
    ) -> str:
        return _dump(
            _retrieve(
                query,
                scope=scope,
                channels=channels,
                top_k=top_k,
                rrf_k=rrf_k,
                include_content=include_content,
                verbose=verbose,
                root=root,
            )
        )

    @server.tool(
        description=(
            "Walk the wikilink graph out from one page. 'seed' accepts a page id, a "
            "path or a title; neighbours are returned with their hop distance."
        )
    )
    @_surface_errors
    def graph_neighbors(seed: str, depth: int = 1, top_k: int = 10) -> str:
        return _dump(neighbors(root, seed, depth, top_k))

    @server.tool(
        description=(
            "Return the deterministic catalogue of the knowledge base: totals, wiki "
            "pages grouped by category with their vector chunk ids, raw source ids, "
            "and the freshness of the vector index."
        )
    )
    @_surface_errors
    def knowledge_tree() -> str:
        return _dump(catalog(root))

    return server


def main() -> None:
    # Corpus loading and the lancedb/pyarrow imports run with stdout diverted, so
    # anything they print cannot corrupt the JSON-RPC stream.
    with contextlib.redirect_stdout(sys.stderr):
        from . import embedding, loader

        root = resolve_root()
        embedding.load_dotenv(root)
        corpus = loader.get_corpus(root)
        server = build_server(root)
        logger.warning(
            "serving %s from %s (%d wiki page(s), %d source file(s))",
            SERVER_NAME,
            root,
            len(corpus.wiki_docs),
            len(corpus.source_docs),
        )
    server.run("stdio")


if __name__ == "__main__":
    main()
