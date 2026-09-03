# LanceDB 读写
"""LanceDB vector store: heading-aware chunks and wholesale rebuild.

`search` returns [] when the table is absent, which is what makes the whole
retrieval path work offline with no index built yet.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

TABLE = "wiki_chunks_v3"
DB_DIRNAME = ".lancedb"

_METADATA_COLUMNS = ["chunk_id", "page_id", "chunk_index", "heading_path"]
# lancedb 0.37 warns that implicit `_distance` autoprojection will be dropped.
_SEARCH_COLUMNS = _METADATA_COLUMNS + ["chunk_text", "_distance"]


def db_path(root: Path) -> str:
    text = str(root)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return f"{text.replace(chr(92), '/')}/{DB_DIRNAME}"


def arrow_schema(dim: int):
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("chunk_id", pa.utf8(), nullable=False),
            pa.field("page_id", pa.utf8(), nullable=False),
            pa.field("chunk_index", pa.uint32(), nullable=False),
            pa.field("chunk_text", pa.utf8(), nullable=False),
            pa.field("heading_path", pa.utf8(), nullable=False),
            pa.field(
                "vector",
                pa.list_(pa.field("item", pa.float32(), nullable=True), dim),
                nullable=False,
            ),
        ]
    )


def _table_names(db: Any) -> list[str]:
    names: list[str] = []
    page_token: Optional[str] = None
    seen: set[str] = set()
    while True:
        response = db.list_tables(page_token=page_token)
        names.extend(response.tables or [])
        page_token = response.page_token
        if not page_token or page_token in seen:
            return names
        seen.add(page_token)


def _connect(root: Path):
    import lancedb

    return lancedb.connect(db_path(root))


def _open_table(root: Path) -> Optional[Any]:
    if not (Path(root) / DB_DIRNAME).is_dir():
        return None
    db = _connect(root)
    if TABLE not in _table_names(db):
        return None
    return db.open_table(TABLE)


def _validate_query(query: list[float]) -> list[float]:
    try:
        vector = [float(value) for value in query]
    except (TypeError, ValueError) as error:
        raise ValueError("query vector must be non-empty and finite") from error
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("query vector must be non-empty and finite")
    return vector


def search(root: Path, query: list[float], top_k: int) -> list[dict]:
    """Return chunk hits; page diversity is enforced by the retrieval channel."""
    vector = _validate_query(query)
    top_k = int(top_k)
    if top_k <= 0:
        return []
    table = _open_table(root)
    if table is None:
        return []
    rows = (
        table.search(vector)
        .limit(top_k)
        .select(_SEARCH_COLUMNS)
        .to_arrow()
        .to_pylist()
    )
    hits = []
    for row in rows:
        hits.append({
            "chunk_id": row["chunk_id"],
            "page_id": row["page_id"],
            "chunk_index": int(row["chunk_index"]),
            "heading_path": row["heading_path"],
            "chunk_text": row["chunk_text"],
            "score": 1.0 / (1.0 + float(row["_distance"])),
        })
    hits.sort(key=lambda hit: (-hit["score"], hit["page_id"], hit["chunk_index"]))
    return hits[:top_k]


def list_chunks(root: Path) -> list[dict]:
    table = _open_table(root)
    if table is None:
        return []
    rows = table.search().limit(None).select(_METADATA_COLUMNS).to_arrow().to_pylist()
    chunks = [
        {
            "chunk_id": row["chunk_id"],
            "page_id": row["page_id"],
            "chunk_index": int(row["chunk_index"]),
            "heading_path": row["heading_path"],
        }
        for row in rows
    ]
    chunks.sort(key=lambda c: (c["page_id"], c["chunk_index"], c["chunk_id"]))
    return chunks


def replace_all(root: Path, rows: list[dict], dim: int) -> None:
    import pyarrow as pa

    if not rows:
        return
    dim = int(dim)
    if dim <= 0:
        raise ValueError("vector dimension must be positive")

    flat: list[float] = []
    for row in rows:
        vector = row["vector"]
        if len(vector) != dim or any(not math.isfinite(float(v)) for v in vector):
            raise ValueError("all vectors must have the same non-zero finite dimension")
        flat.extend(float(v) for v in vector)

    schema = arrow_schema(dim)
    table = pa.Table.from_arrays(
        [
            pa.array([str(row["chunk_id"]) for row in rows], type=pa.utf8()),
            pa.array([str(row["page_id"]) for row in rows], type=pa.utf8()),
            pa.array([int(row["chunk_index"]) for row in rows], type=pa.uint32()),
            pa.array([str(row["chunk_text"]) for row in rows], type=pa.utf8()),
            pa.array([str(row["heading_path"]) for row in rows], type=pa.utf8()),
            pa.FixedSizeListArray.from_arrays(pa.array(flat, type=pa.float32()), dim),
        ],
        schema=schema,
    )
    if not table.schema.equals(schema):
        raise ValueError("constructed Arrow table does not match the expected schema")

    db = _connect(root)
    db.drop_table(TABLE, ignore_missing=True)
    db.create_table(TABLE, table)
