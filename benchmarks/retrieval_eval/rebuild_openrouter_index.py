"""Resumably rebuild a Mobilework index with the shared OpenRouter model.

The checkpoint is an experiment artifact, not a content-integrity manifest. It
only avoids discarding successful embedding batches when the remote endpoint
has a transient failure.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from wiki_retrieval import embedding, index, loader, store


def load_checkpoint(path: Path) -> dict[str, list[float]]:
    vectors: dict[str, list[float]] = {}
    if not path.is_file():
        return vectors
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        vector = [float(value) for value in row["vector"]]
        if len(vector) == 4096:
            vectors[str(row["chunk_id"])] = vector
    return vectors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--attempts", type=int, default=6)
    args = parser.parse_args()

    root = args.project.resolve()
    checkpoint = args.checkpoint.resolve()
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    embedding.load_dotenv(root)
    rows = index._rows(root, None)
    cached = load_checkpoint(checkpoint)
    missing = [row for row in rows if row["chunk_id"] not in cached]
    print(f"rows={len(rows)} cached={len(cached)} missing={len(missing)}", flush=True)

    for start in range(0, len(missing), max(1, args.batch)):
        batch = missing[start : start + max(1, args.batch)]
        last_error: Exception | None = None
        for attempt in range(1, args.attempts + 1):
            try:
                vectors, elapsed_ms = embedding.fetch_batch([row["embedding_text"] for row in batch])
                with checkpoint.open("a", encoding="utf-8") as handle:
                    for row, vector in zip(batch, vectors, strict=True):
                        handle.write(json.dumps({"chunk_id": row["chunk_id"], "vector": vector}) + "\n")
                        cached[row["chunk_id"]] = vector
                print(f"embedded={len(cached)}/{len(rows)} batch_ms={elapsed_ms:.1f}", flush=True)
                break
            except Exception as exc:
                last_error = exc
                print(f"retry={attempt}/{args.attempts} error={type(exc).__name__}", flush=True)
                if attempt < args.attempts:
                    time.sleep(min(8.0, float(1 << (attempt - 1))))
        else:
            raise RuntimeError(f"embedding batch failed after {args.attempts} attempts") from last_error

    for row in rows:
        row["vector"] = cached[row["chunk_id"]]
    store.replace_all(root, rows, 4096)
    meta = index.write_meta(root, 4096, len(rows), len(loader.load_corpus(root).wiki_docs))
    print(json.dumps({"project": str(root), **meta}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
