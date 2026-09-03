# 向量索引构建与新鲜度：写 index_built_at，比对 wiki 最大 mtime 输出 stale_index 标记
"""Vector index build and freshness for heading-aware Wiki chunks.

The table is rebuilt wholesale so edits, deletions, embedding-model changes and
chunking-policy changes cannot leave mixed generations behind.

`freshness` is imported lazily by the maintainer CLI's doctor/commit path, so it
must stay offline and free of side effects — it only reads the meta file and
stats the corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import embedding, loader, store
from .chunking import (
    WIKI_MAX_CHARS,
    WIKI_MIN_HEADING_SPLIT_CHARS,
    WIKI_OVERLAP_CHARS,
    WIKI_TARGET_CHARS,
)

CHUNKING_STRATEGY = "markdown_chunk_v2"
DEFAULT_BATCH = 16
META_FILENAME = "index_meta.json"


def meta_path(root: Path) -> Path:
    return Path(root) / store.DB_DIRNAME / META_FILENAME


def read_meta(root: Path) -> dict | None:
    path = meta_path(root)
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def freshness(root: Path) -> dict:
    """`stale_index` is true whenever a wiki page is newer than the index, and
    also when no index exists at all — both mean vector hits cannot be trusted."""
    root = Path(root)
    corpus_max_mtime = loader.scan_max_mtime(root, "wiki")
    meta = read_meta(root)
    if meta is None:
        return {
            "exists": False,
            "stale_index": True,
            "reason": "no vector index has been built",
            "corpus_max_mtime": corpus_max_mtime,
        }
    built_mtime = float(meta.get("corpus_max_mtime") or 0.0)
    stale = corpus_max_mtime > built_mtime
    expected_parameters = {
        "target_chars": WIKI_TARGET_CHARS,
        "max_chars": WIKI_MAX_CHARS,
        "overlap_chars": WIKI_OVERLAP_CHARS,
        "min_heading_split_chars": WIKI_MIN_HEADING_SPLIT_CHARS,
    }
    strategy_stale = meta.get("chunking_strategy") != CHUNKING_STRATEGY
    parameters_stale = meta.get("chunking_parameters") != expected_parameters
    stale = stale or strategy_stale or parameters_stale
    payload = {
        "exists": True,
        "stale_index": stale,
        "index_built_at": meta.get("index_built_at"),
        "model": meta.get("model"),
        "dim": meta.get("dim"),
        "rows": meta.get("rows"),
        "wiki_pages": meta.get("wiki_pages"),
        "chunking_strategy": meta.get("chunking_strategy"),
        "chunking_parameters": meta.get("chunking_parameters"),
        "corpus_max_mtime": corpus_max_mtime,
        "indexed_max_mtime": built_mtime,
    }
    if stale:
        reasons = []
        if strategy_stale:
            reasons.append("chunking strategy changed")
        if parameters_stale:
            reasons.append("chunking parameters changed")
        if corpus_max_mtime > built_mtime:
            reasons.append("wiki pages changed after the index was built")
        payload["reason"] = "; ".join(reasons)
    return payload


def _rows(root: Path, limit: int | None) -> list[dict]:
    docs = loader.load_corpus(root).wiki_docs
    if limit is not None:
        docs = docs[:limit]
    return [
        {
            "chunk_id": chunk["chunk_id"],
            "page_id": doc.page_id,
            "chunk_index": chunk["chunk_index"],
            "chunk_text": chunk["text"],
            "embedding_text": chunk["embedding_text"],
            "heading_path": chunk["heading_path"],
        }
        for doc in docs
        for chunk in doc.chunks
    ]


def write_meta(root: Path, dim: int, rows: int, wiki_pages: int | None = None) -> dict:
    meta = {
        "index_built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "model": embedding.model_name(),
        "dim": dim,
        "rows": rows,
        "wiki_pages": wiki_pages if wiki_pages is not None else len(loader.load_corpus(root).wiki_docs),
        "chunking_strategy": CHUNKING_STRATEGY,
        "chunking_parameters": {
            "target_chars": WIKI_TARGET_CHARS,
            "max_chars": WIKI_MAX_CHARS,
            "overlap_chars": WIKI_OVERLAP_CHARS,
            "min_heading_split_chars": WIKI_MIN_HEADING_SPLIT_CHARS,
        },
        "corpus_max_mtime": loader.scan_max_mtime(root, "wiki"),
    }
    path = meta_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def build(
    root: Path,
    limit: int | None = None,
    batch: int = DEFAULT_BATCH,
    progress=_log,
) -> dict:
    """Embed every Wiki chunk and replace the table. Requires a live endpoint."""
    root = Path(root)
    embedding.load_dotenv(root)
    rows = _rows(root, limit)
    if not rows:
        raise loader.CorpusEmptyError(f"no wiki pages found under {root / loader.WIKI_DIR}")
    if not embedding.has_api_key():
        raise RuntimeError("EMBEDDING_API_KEY is not set; indexing needs a live endpoint")

    batch_size = max(1, int(batch))
    vectors: list[list[float]] = []
    total_ms = 0.0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        batch_vectors, elapsed = embedding.fetch_batch(
            [row["embedding_text"] for row in chunk]
        )
        vectors.extend(batch_vectors)
        total_ms += elapsed
        progress(
            f"embedded {start + len(chunk)}/{len(rows)} chunk(s) "
            f"({elapsed:.0f} ms this batch)"
        )

    dim = len(vectors[0])
    for row, vector in zip(rows, vectors, strict=True):
        if len(vector) != dim:
            raise RuntimeError(
                f"dimension mismatch on {row['page_id']}: {len(vector)} != {dim}"
            )
        row["vector"] = vector

    store.replace_all(root, rows, dim)
    meta = write_meta(root, dim, len(rows), len(loader.load_corpus(root).wiki_docs))
    meta["embedding_ms"] = total_ms
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki_retrieval.index")
    parser.add_argument("--project", default=None, help="knowledge base root")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true", help="print freshness and exit")
    args = parser.parse_args(argv)

    root = (
        Path(args.project).expanduser().resolve()
        if args.project
        else loader.find_root()
    )
    embedding.load_dotenv(root)

    if args.status:
        _log(json.dumps(freshness(root), ensure_ascii=False, indent=2))
        return 0

    if args.dry_run:
        rows = _rows(root, args.limit)
        if not rows:
            _log("no wiki pages found; nothing to index")
            return 1
        _log(f"{len(rows)} chunk(s) would be indexed from {root}:")
        for row in rows:
            _log(f"  {row['page_id']:<44} {len(row['chunk_text']):>6} chars")
        return 0

    try:
        meta = build(root, args.limit, args.batch)
    except (loader.CorpusEmptyError, RuntimeError) as error:
        _log(str(error))
        return 1
    _log(
        f"indexed {meta['rows']} chunk(s), dim={meta['dim']}, model={meta['model']}, "
        f"embedding {meta['embedding_ms']:.0f} ms -> {store.db_path(root)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
