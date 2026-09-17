from __future__ import annotations

import json
import os
import gc
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import lancedb

from . import config as config_module
from .errors import RetrievalError, require
from .markdown import MarkdownDocument, chunk_document, normalize_text, scan_documents, search_tokens
from .openrouter import OpenRouterClient


SCHEMA_VERSION = 2
VECTOR_TABLE_NAME = "chunks"


def index_dir(state_dir: Path, wiki_id: str) -> Path:
    return Path(state_dir) / "indexes" / wiki_id


def database_path(state_dir: Path, wiki_id: str) -> Path:
    return index_dir(state_dir, wiki_id) / "index.sqlite3"


def vector_database_path(state_dir: Path, wiki_id: str) -> Path:
    return index_dir(state_dir, wiki_id) / "vectors.lancedb"


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            title TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            entity_type TEXT,
            sha256 TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(document_id),
            ordinal INTEGER NOT NULL,
            heading_path TEXT NOT NULL,
            text TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            vector_row INTEGER
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, terms, tokenize='unicode61');
        CREATE TABLE links (
            source_document_id TEXT NOT NULL,
            target_text TEXT NOT NULL,
            target_document_id TEXT,
            UNIQUE(source_document_id, target_text)
        );
        CREATE TABLE entities (
            normalized_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            document_id TEXT NOT NULL,
            entity_type TEXT,
            source TEXT NOT NULL,
            UNIQUE(normalized_name, document_id, source)
        );
        CREATE INDEX chunks_document_idx ON chunks(document_id);
        CREATE INDEX links_source_idx ON links(source_document_id);
        CREATE INDEX links_target_idx ON links(target_document_id);
        CREATE INDEX entities_name_idx ON entities(normalized_name);
        """
    )


def _read_old(state_dir: Path, wiki_id: str, expected_config_fp: str) -> tuple[dict[str, tuple[str, np.ndarray]], int]:
    db = database_path(state_dir, wiki_id)
    vector_db_path = vector_database_path(state_dir, wiki_id)
    if not db.is_file() or not vector_db_path.is_dir():
        return {}, 0
    try:
        connection = sqlite3.connect(db)
        meta = {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM meta")}
        if meta.get("config_fingerprint") != expected_config_fp:
            connection.close()
            return {}, 0
        connection.close()
        vector_db = lancedb.connect(str(vector_db_path))
        if VECTOR_TABLE_NAME not in vector_db.list_tables().tables:
            return {}, 0
        table = vector_db.open_table(VECTOR_TABLE_NAME)
        rows = table.to_arrow().select(["chunk_id", "sha256", "vector"]).to_pylist()
        result = {
            str(row["chunk_id"]): (str(row["sha256"]), np.asarray(row["vector"], dtype=np.float32))
            for row in rows
        }
        dimension = int(next(iter(result.values()))[1].size) if result else 0
        return result, dimension
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        return {}, 0


def _replace_vector_database(temporary: Path | None, target: Path) -> None:
    backup = target.with_name(f"{target.name}.backup")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        os.replace(target, backup)
    try:
        if temporary is not None:
            os.replace(temporary, target)
    except Exception:
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _normalize_vector(values: list[float] | np.ndarray) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    require(vector.ndim == 1 and vector.size > 0 and np.isfinite(vector).all() and norm > 0,
            "VECTOR_UNAVAILABLE", "embedding 向量无效")
    return vector / norm


def _resolve_links(documents: list[MarkdownDocument]) -> list[tuple[str, str, str | None]]:
    by_title: dict[str, list[str]] = {}
    by_path: dict[str, str] = {}
    for document in documents:
        names = [document.title, *document.aliases, Path(document.relative_path).stem]
        for name in names:
            by_title.setdefault(normalize_text(name), []).append(document.document_id)
        by_path[normalize_text(document.relative_path)] = document.document_id
        by_path[normalize_text(str(Path(document.relative_path).with_suffix("")))] = document.document_id
    links = []
    for document in documents:
        for raw in document.links:
            candidate = raw.replace("\\", "/").strip()
            normalized = normalize_text(candidate)
            target = by_path.get(normalized) or by_path.get(normalize_text(candidate.removesuffix(".md")))
            if target is None:
                matches = list(dict.fromkeys(by_title.get(normalize_text(Path(candidate).stem), [])))
                target = matches[0] if len(matches) == 1 else None
            links.append((document.document_id, raw, target))
    return links


def build_index(entry: dict[str, Any], state_dir: Path, config: dict[str, Any],
                client: OpenRouterClient | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    documents, report = scan_documents(entry)
    require(bool(documents), "NO_DOCUMENTS", f"Wiki 中没有匹配的 Markdown 文件：{entry['root']}")
    target_dir = index_dir(state_dir, entry["wiki_id"])
    target_dir.mkdir(parents=True, exist_ok=True)
    config_fp = config_module.fingerprint(config)
    old, old_dim = _read_old(state_dir, entry["wiki_id"], config_fp)
    indexing = config["indexing"]
    chunks = [
        chunk
        for document in documents
        for chunk in chunk_document(
            document, int(indexing["chunk_size_chars"]), int(indexing["chunk_overlap_chars"]),
            bool(indexing["include_heading_path"]),
        )
    ]
    reusable: dict[str, np.ndarray] = {}
    pending: list[dict[str, Any]] = []
    for chunk in chunks:
        previous = old.get(chunk["chunk_id"])
        if previous is not None and previous[0] == chunk["sha256"]:
            reusable[chunk["chunk_id"]] = previous[1]
        else:
            pending.append(chunk)

    client = client or OpenRouterClient(config)
    embedded: dict[str, np.ndarray] = {}
    embedding_status: dict[str, Any] = {
        "requested": len(pending), "reused": len(reusable), "status": "available" if reusable else "disabled",
        "model": config["embedding"]["remote"]["model"],
    }
    if pending and client.embedding_available():
        batch_size = int(config["embedding"]["remote"]["batch_size"])
        total_ms = 0.0
        try:
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset:offset + batch_size]
                vectors, elapsed = client.embed([item["embedding_text"] for item in batch], query=False)
                total_ms += elapsed
                for chunk, vector in zip(batch, vectors, strict=True):
                    embedded[chunk["chunk_id"]] = _normalize_vector(vector)
            embedding_status.update(status="available", embedded=len(embedded), latency_ms=total_ms)
        except RetrievalError as error:
            embedding_status.update(status="degraded", error=error.as_dict())
    elif pending:
        embedding_status.update(status="disabled", reason="OPENROUTER_API_KEY 未设置")

    vectors_by_id = {**reusable, **embedded}
    dimensions = {int(vector.size) for vector in vectors_by_id.values()}
    if len(dimensions) > 1:
        vectors_by_id = embedded
        reusable = {}
        dimensions = {int(vector.size) for vector in vectors_by_id.values()}
        embedding_status.update(status="degraded", reason="旧向量维度不兼容，已丢弃复用向量")
    vector_dim = next(iter(dimensions), old_dim if not vectors_by_id else 0)
    ordered_vectors: list[tuple[str, str, np.ndarray]] = []
    vector_rows: dict[str, int] = {}
    for chunk in chunks:
        vector = vectors_by_id.get(chunk["chunk_id"])
        if vector is not None:
            vector_rows[chunk["chunk_id"]] = len(ordered_vectors)
            ordered_vectors.append((chunk["chunk_id"], chunk["sha256"], vector))

    temporary_db = target_dir / "index.sqlite3.tmp"
    temporary_vectors = target_dir / "vectors.lancedb.tmp"
    if temporary_db.exists():
        temporary_db.unlink()
    if temporary_vectors.exists():
        shutil.rmtree(temporary_vectors)
    connection = sqlite3.connect(temporary_db)
    try:
        _schema(connection)
        document_map = {document.document_id: document for document in documents}
        connection.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(doc.document_id, doc.relative_path, doc.title, json.dumps(doc.aliases, ensure_ascii=False),
              doc.entity_type, doc.sha256, doc.mtime_ns, doc.size) for doc in documents],
        )
        connection.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(chunk["chunk_id"], chunk["document_id"], chunk["ordinal"], chunk["heading_path"],
              chunk["text"], chunk["sha256"], vector_rows.get(chunk["chunk_id"])) for chunk in chunks],
        )
        fts_rows = []
        for chunk in chunks:
            document = document_map[chunk["document_id"]]
            terms = search_tokens(" ".join([document.title, *document.aliases, chunk["heading_path"], chunk["text"]]))
            fts_rows.append((chunk["chunk_id"], " ".join(terms)))
        connection.executemany("INSERT INTO chunks_fts(chunk_id, terms) VALUES (?, ?)", fts_rows)
        connection.executemany("INSERT OR IGNORE INTO links VALUES (?, ?, ?)", _resolve_links(documents))
        entities = []
        for document in documents:
            entities.append((normalize_text(document.title), document.title, document.document_id,
                             document.entity_type, "title"))
            entities.extend((normalize_text(alias), alias, document.document_id, document.entity_type, "alias")
                            for alias in document.aliases)
        connection.executemany("INSERT OR IGNORE INTO entities VALUES (?, ?, ?, ?, ?)", entities)
        built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        root_max_mtime = max(document.mtime_ns for document in documents)
        meta = {
            "schema_version": SCHEMA_VERSION, "wiki_id": entry["wiki_id"], "built_at": built_at,
            "config_fingerprint": config_fp, "embedding_model": config["embedding"]["remote"]["model"],
            "vector_dim": vector_dim, "vector_rows": len(ordered_vectors), "root_max_mtime_ns": root_max_mtime,
            "vector_backend": "lancedb", "vector_table": VECTOR_TABLE_NAME,
            "document_count": len(documents), "chunk_count": len(chunks), "scan_report": report,
        }
        connection.executemany("INSERT INTO meta VALUES (?, ?)",
                               [(key, json.dumps(value, ensure_ascii=False)) for key, value in meta.items()])
        connection.commit()
    finally:
        connection.close()

    vector_store = config["vector_store"]
    ann_index_created = False
    if ordered_vectors:
        vector_db = lancedb.connect(str(temporary_vectors))
        table = vector_db.create_table(
            VECTOR_TABLE_NAME,
            data=[
                {"chunk_id": chunk_id, "sha256": sha256, "vector": vector.tolist()}
                for chunk_id, sha256, vector in ordered_vectors
            ],
            mode="overwrite",
        )
        if vector_store["create_ann_index"] and len(ordered_vectors) >= int(vector_store["ann_min_rows"]):
            table.create_index(
                metric=vector_store["metric"],
                index_type=vector_store["ann_index_type"],
                target_partition_size=int(vector_store["target_partition_size"]),
                replace=True,
            )
            ann_index_created = True
        del table, vector_db
        gc.collect()
    os.replace(temporary_db, database_path(state_dir, entry["wiki_id"]))
    _replace_vector_database(temporary_vectors if ordered_vectors else None,
                             vector_database_path(state_dir, entry["wiki_id"]))
    return {
        "status": "ok" if embedding_status["status"] == "available" else "partial",
        "wiki_id": entry["wiki_id"], "documents": len(documents), "chunks": len(chunks),
        "vectors": len(ordered_vectors), "reused_vectors": len(reusable),
        "vector_store": {"backend": "lancedb", "table": VECTOR_TABLE_NAME,
                         "metric": vector_store["metric"], "ann_index_created": ann_index_created},
        "embedding": embedding_status, "scan": report,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    }


class IndexStore:
    def __init__(self, state_dir: Path, wiki_id: str):
        self.wiki_id = wiki_id
        self.db_path = database_path(state_dir, wiki_id)
        self.vector_path = vector_database_path(state_dir, wiki_id)
        require(self.db_path.is_file(), "INDEX_STALE", f"Wiki 尚未建立索引：{wiki_id}")
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.vector_db = None
        self.vector_table = None
        if self.vector_path.is_dir():
            self.vector_db = lancedb.connect(str(self.vector_path))
            if VECTOR_TABLE_NAME in self.vector_db.list_tables().tables:
                self.vector_table = self.vector_db.open_table(VECTOR_TABLE_NAME)

    def close(self) -> None:
        self.connection.close()
        self.vector_table = None
        self.vector_db = None

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def meta(self) -> dict[str, Any]:
        return {row["key"]: json.loads(row["value"]) for row in self.connection.execute("SELECT key, value FROM meta")}

    def _hydrate(self, chunk_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(chunk_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT c.*, d.relative_path, d.title, d.aliases_json, d.entity_type "
            f"FROM chunks c JOIN documents d ON d.document_id=c.document_id WHERE c.chunk_id IN ({placeholders})", ids,
        ).fetchall()
        by_id = {row["chunk_id"]: dict(row) for row in rows}
        result = []
        for chunk_id in ids:
            if chunk_id in by_id:
                item = by_id[chunk_id]
                item["aliases"] = json.loads(item.pop("aliases_json"))
                item["wiki_id"] = self.wiki_id
                result.append(item)
        return result

    def keyword_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        tokens = search_tokens(query)
        if not tokens:
            return []
        expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:64])
        rows = self.connection.execute(
            "SELECT chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (expression, int(limit)),
        ).fetchall()
        hydrated = self._hydrate([row["chunk_id"] for row in rows])
        ranks = {row["chunk_id"]: float(row["rank"]) for row in rows}
        for item in hydrated:
            rank = ranks[item["chunk_id"]]
            item["score"] = -rank if rank < 0 else 1.0 / (1.0 + rank)
        return hydrated

    def vector_search(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        if self.vector_table is None:
            return []
        query = _normalize_vector(vector)
        meta = self.meta()
        if query.size != int(meta.get("vector_dim", 0)):
            raise RetrievalError("VECTOR_UNAVAILABLE", "查询向量与索引维度不一致")
        rows = (
            self.vector_table.search(query.tolist())
            .distance_type("cosine")
            .select(["chunk_id", "_distance"])
            .limit(int(limit))
            .to_list()
        )
        ordered_ids = [str(row["chunk_id"]) for row in rows]
        hydrated = self._hydrate(ordered_ids)
        by_score = {str(row["chunk_id"]): 1.0 - float(row["_distance"]) for row in rows}
        for item in hydrated:
            item["score"] = by_score[item["chunk_id"]]
        return hydrated

    def entity_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        normalized = normalize_text(query)
        rows = self.connection.execute(
            "SELECT normalized_name, display_name, document_id, entity_type, source FROM entities ORDER BY source DESC, display_name"
        ).fetchall()
        matches = [row for row in rows if row["normalized_name"] and (
            row["normalized_name"] in normalized or normalized in row["normalized_name"]
        )]
        document_ids = list(dict.fromkeys(row["document_id"] for row in matches))[:limit]
        if not document_ids:
            return []
        placeholders = ",".join("?" for _ in document_ids)
        chunk_rows = self.connection.execute(
            f"SELECT chunk_id FROM chunks WHERE document_id IN ({placeholders}) ORDER BY document_id, ordinal",
            document_ids,
        ).fetchall()
        hydrated = self._hydrate([row["chunk_id"] for row in chunk_rows[:limit]])
        for item in hydrated:
            item["score"] = 1.0
        return hydrated

    def graph_neighbors(self, seed_document_id: str, depth: int, limit: int) -> list[dict[str, Any]]:
        require(depth >= 0, "INVALID_ARGUMENT", "图深度不能为负数")
        seen = {seed_document_id}
        frontier = {seed_document_id}
        distance: dict[str, int] = {}
        for hop in range(1, depth + 1):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            rows = self.connection.execute(
                f"SELECT source_document_id, target_document_id FROM links "
                f"WHERE target_document_id IS NOT NULL AND (source_document_id IN ({placeholders}) "
                f"OR target_document_id IN ({placeholders}))", [*frontier, *frontier],
            ).fetchall()
            next_frontier: set[str] = set()
            for row in rows:
                for candidate in (row["source_document_id"], row["target_document_id"]):
                    if candidate not in seen:
                        seen.add(candidate)
                        next_frontier.add(candidate)
                        distance[candidate] = hop
            frontier = next_frontier
        ordered_docs = sorted(distance, key=lambda item: (distance[item], item))[:limit]
        if not ordered_docs:
            return []
        placeholders = ",".join("?" for _ in ordered_docs)
        rows = self.connection.execute(
            f"SELECT chunk_id, document_id FROM chunks WHERE ordinal=0 AND document_id IN ({placeholders})", ordered_docs
        ).fetchall()
        by_doc = {row["document_id"]: row["chunk_id"] for row in rows}
        hydrated = self._hydrate([by_doc[doc] for doc in ordered_docs if doc in by_doc])
        for item in hydrated:
            item["distance"] = distance[item["document_id"]]
            item["score"] = 1.0 / (1 + item["distance"])
        return hydrated

    def resolve_seed(self, document_id: str | None = None, relative_path: str | None = None,
                     title: str | None = None) -> str:
        if document_id:
            row = self.connection.execute("SELECT document_id FROM documents WHERE document_id=?", (document_id,)).fetchone()
            require(row is not None, "AMBIGUOUS_SEED", "未找到 seed 文档")
            return str(row["document_id"])
        if relative_path:
            row = self.connection.execute("SELECT document_id FROM documents WHERE relative_path=?", (relative_path,)).fetchone()
            require(row is not None, "AMBIGUOUS_SEED", "未找到 seed 路径")
            return str(row["document_id"])
        require(bool(title), "INVALID_ARGUMENT", "seed 必须包含 document_id、relative_path 或 title")
        rows = self.connection.execute(
            "SELECT DISTINCT document_id FROM entities WHERE normalized_name=?", (normalize_text(title or ""),)
        ).fetchall()
        require(len(rows) == 1, "AMBIGUOUS_SEED", "seed 标题不存在或不唯一",
                candidates=[row["document_id"] for row in rows])
        return str(rows[0]["document_id"])

    def tree(self) -> dict[str, Any]:
        documents = [dict(row) for row in self.connection.execute(
            "SELECT document_id, relative_path, title, entity_type FROM documents ORDER BY relative_path"
        )]
        return {"wiki_id": self.wiki_id, "documents": documents, "index": self.meta()}

    def entity_names(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT normalized_name, display_name, document_id, entity_type, source FROM entities ORDER BY normalized_name"
        )]


def freshness(entry: dict[str, Any], state_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    path = database_path(state_dir, entry["wiki_id"])
    if not path.is_file():
        return {"exists": False, "stale": True, "reason": "index_missing"}
    try:
        with IndexStore(state_dir, entry["wiki_id"]) as store:
            meta = store.meta()
        config_stale = meta.get("config_fingerprint") != config_module.fingerprint(config)
        documents, _ = scan_documents(entry)
        latest = max((doc.mtime_ns for doc in documents), default=0)
        content_stale = latest > int(meta.get("root_max_mtime_ns", 0)) or len(documents) != int(meta.get("document_count", 0))
        reasons = []
        if config_stale:
            reasons.append("config_changed")
        if content_stale:
            reasons.append("wiki_changed")
        return {"exists": True, "stale": bool(reasons), "reasons": reasons, **meta}
    except Exception as error:
        return {"exists": True, "stale": True, "reason": f"index_unreadable:{type(error).__name__}"}
