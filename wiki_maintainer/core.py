from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .frontmatter import ManagedPage, parse_managed_page, replace_list_field, replace_source_path
from .incremental import (
    bm25_candidates,
    chunk_text,
    diff_chunks,
    normalize_identity,
    page_documents,
    parse_claims,
    prune_claims,
)


DEFAULT_CONFIG: dict[str, Any] = {
    "sources_dir": "raw/sources",
    "wiki_dir": "wiki",
    "state_dir": ".wiki-state",
    "trash_dir": ".wiki-trash",
    "language": "zh-CN",
    # Bump this when the semantic ontology/prompt changes. Active sources
    # compiled by an older revision are re-analysed without touching raw files.
    "semantic_revision": 2,
    # 二进制原件由解析组转成 md 后落到 raw/sources，构建侧只吃纯文本
    "include_extensions": [".md", ".markdown", ".txt"],
    "ignore_names": [".DS_Store", "Thumbs.db"],
    "watch": {
        "interval_seconds": 2.0,
        "settle_seconds": 1.0,
        "retry_seconds": 30.0,
        "agent_command": ["opencode", "run", "--format", "json", "--dir", "{root}", "{prompt}"],
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_source(path: Path) -> str:
    """读取解析组产出的纯文本 source。

    gb18030 兜底是因为历史中文语料常见非 UTF-8 编码；三次都失败时用 replace
    降级而不是抛错，个别坏字节不该阻塞整个 batch。
    """
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    sources: Path
    wiki: Path
    state: Path
    trash: Path
    manifest: Path
    pending: Path
    lock: Path


def load_config(root: Path) -> dict[str, Any]:
    path = root / "wiki.config.json"
    user = load_json(path, {})
    config = dict(DEFAULT_CONFIG)
    config.update({key: value for key, value in user.items() if key != "watch"})
    config["watch"] = {**DEFAULT_CONFIG["watch"], **user.get("watch", {})}
    return config


def paths_for(root: Path) -> ProjectPaths:
    root = root.resolve()
    config = load_config(root)
    state = root / config["state_dir"]
    return ProjectPaths(
        root=root,
        sources=root / config["sources_dir"],
        wiki=root / config["wiki_dir"],
        state=state,
        trash=root / config["trash_dir"],
        manifest=state / "manifest.json",
        pending=state / "pending.json",
        lock=state / "sync.lock",
    )


@contextmanager
def project_lock(paths: ProjectPaths, stale_seconds: int = 1800) -> Iterator[None]:
    paths.state.mkdir(parents=True, exist_ok=True)
    if paths.lock.exists() and time.time() - paths.lock.stat().st_mtime > stale_seconds:
        paths.lock.unlink()
    try:
        descriptor = os.open(paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError(f"another wiki operation is active: {paths.lock}") from error
    try:
        os.write(descriptor, f"pid={os.getpid()} started={now_iso()}\n".encode())
        os.close(descriptor)
        yield
    finally:
        paths.lock.unlink(missing_ok=True)


def empty_manifest() -> dict[str, Any]:
    return {"version": 3, "updated_at": now_iso(), "sources": {}, "pages": {}, "last_batch": None}


def initialize(root: Path) -> ProjectPaths:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "wiki.config.json"
    if not config_path.exists():
        atomic_json(config_path, DEFAULT_CONFIG)
    paths = paths_for(root)
    for directory in (
        paths.sources, paths.wiki, paths.state, paths.trash,
        paths.wiki / "sources", paths.wiki / "concepts", paths.wiki / "entities",
        paths.wiki / "skills", paths.wiki / "references", paths.wiki / "synthesis",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if not paths.manifest.exists():
        atomic_json(paths.manifest, empty_manifest())
    special = {
        paths.root / "purpose.md": "# Purpose\n\n这个 Wiki 要帮助我积累可追溯、可连接、可持续更新的知识。\n",
        paths.root / "schema.md": (
            "# Schema\n\nWiki 页面放在 `wiki/`，使用 Obsidian wikilink。"
            "所有自动生成页必须声明 `wiki_managed: true`、`source_ids` 与 `sources`。\n"
        ),
        paths.wiki / "index.md": "# Wiki Index\n",
        paths.wiki / "log.md": "# Wiki Log\n",
    }
    for path, content in special.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    return paths


def _source_snapshot(paths: ProjectPaths, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    allowed = {item.lower() for item in config["include_extensions"]}
    ignored = set(config["ignore_names"])
    snapshot: dict[str, dict[str, Any]] = {}
    if not paths.sources.exists():
        return snapshot
    for path in sorted(paths.sources.rglob("*")):
        if not path.is_file() or path.name in ignored or path.suffix.lower() not in allowed:
            continue
        stat = path.stat()
        rel = path.relative_to(paths.root).as_posix()
        snapshot[rel] = {
            "path": rel,
            "sha256": file_hash(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    return snapshot


def _new_source_id() -> str:
    return "src_" + uuid.uuid4().hex[:16]


def _compute_events(
    manifest: dict[str, Any],
    current: dict[str, dict[str, Any]],
    semantic_revision: int = 1,
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = manifest["sources"]
    active_by_path = {
        record["path"]: (source_id, record)
        for source_id, record in records.items() if record.get("state") == "active"
    }
    deleted_by_path: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for source_id, record in records.items():
        if record.get("state") == "deleted":
            deleted_by_path.setdefault(record["path"], []).append((source_id, record))
    removed = {path: item for path, item in active_by_path.items() if path not in current}
    added = {path: item for path, item in current.items() if path not in active_by_path}

    removed_by_hash: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    added_by_hash: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for path, (source_id, record) in removed.items():
        removed_by_hash.setdefault(record["sha256"], []).append((path, source_id, record))
    for path, info in added.items():
        added_by_hash.setdefault(info["sha256"], []).append((path, info))

    events: list[dict[str, Any]] = []
    moved_old: set[str] = set()
    moved_new: set[str] = set()
    for digest, old_items in removed_by_hash.items():
        new_items = added_by_hash.get(digest, [])
        if len(old_items) == len(new_items) == 1:
            old_path, source_id, record = old_items[0]
            new_path, info = new_items[0]
            events.append({
                "kind": "moved", "source_id": source_id, "old_path": old_path,
                "path": new_path, "before": record, "after": info,
                "affected_pages": list(record.get("pages", [])),
            })
            moved_old.add(old_path)
            moved_new.add(new_path)

    for path, info in current.items():
        if path in active_by_path:
            source_id, record = active_by_path[path]
            if record["sha256"] != info["sha256"] or int(record.get("semantic_revision", 1)) < semantic_revision:
                events.append({
                    "kind": "modified", "source_id": source_id, "path": path,
                    "before": record, "after": info,
                    "affected_pages": list(record.get("pages", [])),
                    "reason": (
                        "content_changed" if record["sha256"] != info["sha256"]
                        else "semantic_revision_changed"
                    ),
                })
            continue
        if path in moved_new:
            continue
        if path in deleted_by_path:
            matches = [item for item in deleted_by_path[path] if item[1].get("sha256") == info["sha256"]]
            if matches:
                source_id, record = max(matches, key=lambda item: item[1].get("deleted_at") or "")
                events.append({
                    "kind": "restored", "source_id": source_id, "path": path,
                    "before": record, "after": info,
                    "affected_pages": list(record.get("pages", [])),
                })
            else:
                events.append({
                    "kind": "new", "source_id": _new_source_id(), "path": path,
                    "before": None, "after": info, "affected_pages": [],
                    "replaces_tombstones": [item[0] for item in deleted_by_path[path]],
                })
        else:
            events.append({
                "kind": "new", "source_id": _new_source_id(), "path": path,
                "before": None, "after": info, "affected_pages": [],
            })

    for path, (source_id, record) in removed.items():
        if path in moved_old:
            continue
        events.append({
            "kind": "deleted", "source_id": source_id, "path": path,
            "before": record, "after": None,
            "affected_pages": list(record.get("pages", [])),
        })
    for event in events:
        if event["kind"] != "deleted":
            event["semantic_revision"] = semantic_revision
    return sorted(events, key=lambda item: (item["kind"], item["path"]))


def prepare(root: Path) -> dict[str, Any] | None:
    paths = initialize(root)
    with project_lock(paths):
        if paths.pending.exists():
            pending = load_json(paths.pending, {})
            work_order = Path(pending.get("work_order", ""))
            if work_order.exists():
                return load_json(work_order, {})
            paths.pending.unlink(missing_ok=True)

        manifest = load_json(paths.manifest, empty_manifest())
        config = load_config(paths.root)
        semantic_revision = int(config.get("semantic_revision", 1))
        events = _compute_events(
            manifest,
            _source_snapshot(paths, config),
            semantic_revision,
        )
        if not events:
            return None

        batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        batch_dir = paths.state / "batches" / batch_id
        staging_wiki = batch_dir / "staging" / "wiki"
        inputs = batch_dir / "inputs"
        staging_wiki.mkdir(parents=True, exist_ok=True)
        inputs.mkdir(parents=True, exist_ok=True)

        affected = sorted({page for event in events for page in event["affected_pages"]})
        base_hashes: dict[str, str | None] = {}
        for rel in affected:
            source = paths.wiki / rel
            base_hashes[rel] = file_hash(source) if source.exists() else None
            if source.exists():
                destination = staging_wiki / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        deleted_ids = {event["source_id"] for event in events if event["kind"] == "deleted"}
        claim_pruning: dict[str, Any] = {}
        if deleted_ids:
            for rel in affected:
                staged_path = staging_wiki / rel
                if not staged_path.exists():
                    continue
                page = parse_managed_page(staged_path)
                survivors = [item for item in page.source_ids if item not in deleted_ids]
                if not survivors:
                    continue
                original = staged_path.read_text(encoding="utf-8")
                updated, pruning = prune_claims(original, deleted_ids)
                if not pruning["removed_claims"] and not pruning["pruned_claims"]:
                    continue
                survivor_paths = [manifest["sources"][item]["path"] for item in survivors]
                updated = replace_list_field(updated, "source_ids", survivors)
                updated = replace_list_field(updated, "sources", survivor_paths)
                staged_path.write_text(updated, encoding="utf-8")
                claim_pruning[rel] = pruning

        identity_documents = page_documents(paths.wiki)
        for event in events:
            if event["kind"] == "deleted":
                removed_chunks = {item["chunk_id"] for item in event.get("before", {}).get("chunks", [])}
                event["affected_claims"] = sorted({
                    claim["id"]
                    for rel in event.get("affected_pages", [])
                    for claim in manifest.get("pages", {}).get(rel, {}).get("claims", [])
                    if event["source_id"] in claim.get("source_ids", [])
                })
                event["removed_chunk_ids"] = sorted(removed_chunks)
                continue
            source_path = paths.root / event["path"]
            output = inputs / f"{event['source_id']}.md"
            try:
                text = _read_source(source_path)
                output.write_text(
                    f"# Extracted source\n\n- source_id: `{event['source_id']}`\n"
                    f"- source_path: `{event['path']}`\n\n---\n\n{text}", encoding="utf-8"
                )
                event["extracted_path"] = output.relative_to(paths.root).as_posix()
                event["extraction_error"] = None
                new_chunks = chunk_text(text)
                stored_chunks = [
                    {key: value for key, value in item.items() if key != "text"}
                    for item in new_chunks
                ]
                old_chunks = (event.get("before") or {}).get("chunks", [])
                delta = diff_chunks(old_chunks, new_chunks)
                event["chunks"] = stored_chunks
                event["diff"] = {
                    **{key: value for key, value in delta.items() if key not in {"added", "removed", "unchanged"}},
                    "added": delta["added"],
                    "removed": delta["removed"],
                    "unchanged_chunk_ids": [item["chunk_id"] for item in delta["unchanged"]],
                }
                changed_chunk_ids = {
                    item["chunk_id"] for item in delta["added"] + delta["removed"]
                }
                event["affected_claims"] = sorted({
                    claim["id"]
                    for rel in event.get("affected_pages", [])
                    for claim in manifest.get("pages", {}).get(rel, {}).get("claims", [])
                    if set(claim.get("chunk_ids", [])) & changed_chunk_ids
                })
                event["identity_candidates"] = bm25_candidates(text[:12000], identity_documents)
            except Exception as error:  # agent can still inspect the original file
                event["extracted_path"] = None
                event["extraction_error"] = str(error)
                event["chunks"] = []
                event["diff"] = None
                event["affected_claims"] = []
                event["identity_candidates"] = []

        work_order = {
            "version": 3,
            "batch_id": batch_id,
            "created_at": now_iso(),
            "root": str(paths.root),
            "wiki_dir": paths.wiki.relative_to(paths.root).as_posix(),
            "staging_wiki": staging_wiki.relative_to(paths.root).as_posix(),
            "semantic_revision": semantic_revision,
            "analysis_path": (batch_dir / "semantic-analysis.json").relative_to(paths.root).as_posix(),
            "events": events,
            "claim_pruning": claim_pruning,
            "base_hashes": base_hashes,
            "system_base_hashes": {
                name: file_hash(paths.wiki / name) if (paths.wiki / name).exists() else None
                for name in ("index.md", "log.md")
            },
            "result_path": (batch_dir / "result.json").relative_to(paths.root).as_posix(),
            "instructions": [
                "Edit only staging_wiki, never the active wiki.",
                "Reconcile shared pages against surviving source texts when a source changes or disappears.",
                "Every staged page needs wiki_managed: true plus exact source_ids and sources arrays.",
                "Use event.diff and affected_claims for local updates; unchanged chunks need not be reprocessed.",
                "Check identity_candidates before creating a page; merge with an existing identity when appropriate.",
                "Complete semantic-analysis.json before writing pages: classify key entities, concepts, procedures, arguments, and cross-source synthesis opportunities.",
                "A named person, organization, product, tool, place, or law-issuing body is an entity; a report, metric, or topic about an entity is not the entity itself.",
                "Add wiki-claim JSON markers after material claim blocks for claim-level provenance.",
                "Run record-source for each non-deleted event, then run commit.",
            ],
        }
        work_order_path = batch_dir / "work-order.json"
        atomic_json(work_order_path, work_order)
        atomic_json(batch_dir / "result.json", {"version": 1, "batch_id": batch_id, "source_pages": {}})
        atomic_json(paths.pending, {
            "batch_id": batch_id,
            "work_order": str(work_order_path),
            "created_at": now_iso(),
        })
        return work_order


def pending_work_order(root: Path) -> dict[str, Any] | None:
    paths = paths_for(root)
    if not paths.pending.exists():
        return None
    pending = load_json(paths.pending, {})
    path = Path(pending["work_order"])
    return load_json(path, {}) if path.exists() else None


def record_source(root: Path, batch_id: str, source_id: str, pages: list[str]) -> None:
    paths = paths_for(root)
    work = pending_work_order(root)
    if not work or work["batch_id"] != batch_id:
        raise RuntimeError(f"batch is not pending: {batch_id}")
    valid_ids = {event["source_id"] for event in work["events"] if event["kind"] != "deleted"}
    if source_id not in valid_ids:
        raise ValueError(f"source is not processable in this batch: {source_id}")
    clean_pages = sorted({_safe_page_rel(page) for page in pages})
    result_path = paths.root / work["result_path"]
    result = load_json(result_path, {"version": 1, "batch_id": batch_id, "source_pages": {}})
    result["source_pages"][source_id] = clean_pages
    atomic_json(result_path, result)


def checkout_page(root: Path, batch_id: str, page: str) -> dict[str, Any]:
    """Add an existing managed page to a batch's optimistic-lock snapshot."""
    paths = paths_for(root)
    rel = _safe_page_rel(page)
    with project_lock(paths):
        work = pending_work_order(root)
        if not work or work["batch_id"] != batch_id:
            raise RuntimeError(f"batch is not pending: {batch_id}")
        active = paths.wiki / rel
        if not active.exists():
            raise ValueError(f"active wiki page does not exist: {rel}")
        parsed = parse_managed_page(active)
        if not parsed.managed:
            raise ValueError(f"cannot checkout a user-managed page: {rel}")
        staging = paths.root / work["staging_wiki"] / rel
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(active, staging)
        work.setdefault("base_hashes", {})[rel] = file_hash(active)
        pending = load_json(paths.pending, {})
        work_path = Path(pending["work_order"])
        atomic_json(work_path, work)
        return {"status": "checked_out", "page": rel, "base_sha256": work["base_hashes"][rel]}


def _safe_page_rel(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    candidate = Path(normalized)
    if not normalized.endswith(".md") or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe wiki page path: {value}")
    if normalized in {"index.md", "log.md"}:
        raise ValueError(f"reserved wiki page path: {value}")
    return normalized


def _scan_staged(work: dict[str, Any], paths: ProjectPaths) -> dict[str, ManagedPage]:
    staging = paths.root / work["staging_wiki"]
    pages: dict[str, ManagedPage] = {}
    for path in sorted(staging.rglob("*.md")):
        rel = path.relative_to(staging).as_posix()
        if rel in {"index.md", "log.md"}:
            continue
        page = parse_managed_page(path)
        if not page.managed:
            raise ValueError(f"staged page is not marked wiki_managed: {rel}")
        if not page.source_ids or not page.sources:
            raise ValueError(f"staged page has empty provenance: {rel}")
        pages[rel] = page
    return pages


def _validate_semantic_analysis(
    paths: ProjectPaths,
    work: dict[str, Any],
    staged: dict[str, ManagedPage],
) -> None:
    """Require an auditable two-stage plan for semantic-revision batches."""
    if not any(event.get("reason") == "semantic_revision_changed" for event in work["events"]):
        return
    analysis_path = paths.root / work.get("analysis_path", "")
    analysis = load_json(analysis_path, {})
    if analysis.get("batch_id") != work["batch_id"] or analysis.get("version") != 1:
        raise ValueError("semantic analysis missing or has the wrong batch/version")
    source_rows = analysis.get("sources")
    if not isinstance(source_rows, list):
        raise ValueError("semantic analysis sources must be an array")
    required_ids = {event["source_id"] for event in work["events"] if event["kind"] != "deleted"}
    seen_ids = {row.get("source_id") for row in source_rows if isinstance(row, dict)}
    if seen_ids != required_ids:
        raise ValueError("semantic analysis must cover every processable source exactly once")
    required_fields = {
        "source_id", "source_path", "entities", "concepts", "procedures",
        "arguments", "existing_matches", "page_plan", "skipped_candidates",
    }
    planned_pages: set[str] = set()
    for row in source_rows:
        if not required_fields <= set(row):
            raise ValueError(f"semantic analysis entry is incomplete: {row.get('source_id')}")
        for field in required_fields - {"source_id", "source_path"}:
            if not isinstance(row[field], list):
                raise ValueError(f"semantic analysis {field} must be an array: {row['source_id']}")
        for plan in row["page_plan"]:
            if isinstance(plan, dict) and plan.get("action") in {"create", "merge", "replace"}:
                planned_pages.add(_safe_page_rel(plan.get("path", "")))
    unplanned = set(staged) - planned_pages
    if unplanned:
        raise ValueError(f"staged pages missing from semantic page_plan: {sorted(unplanned)}")


def _future_sources(manifest: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    future = {key: dict(value) for key, value in manifest["sources"].items()}
    for event in events:
        source_id = event["source_id"]
        if event["kind"] == "deleted":
            if source_id in future:
                future[source_id].update({"state": "deleted", "deleted_at": now_iso(), "pages": []})
            continue
        after = event["after"]
        previous = future.get(source_id, {})
        future[source_id] = {
            **previous,
            **after,
            "source_id": source_id,
            "path": event["path"],
            "state": "active",
            "updated_at": now_iso(),
            "deleted_at": None,
            "chunks": event.get("chunks", previous.get("chunks", [])),
            "semantic_revision": int(event.get("semantic_revision", previous.get("semantic_revision", 1))),
        }
    return future


def _validate_batch(
    paths: ProjectPaths,
    manifest: dict[str, Any],
    work: dict[str, Any],
    result: dict[str, Any],
    staged: dict[str, ManagedPage],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    events = work["events"]
    future = _future_sources(manifest, events)
    active_ids = {source_id for source_id, record in future.items() if record.get("state") == "active"}
    required_results = {event["source_id"] for event in events if event["kind"] != "deleted"}
    missing_results = required_results - set(result.get("source_pages", {}))
    if missing_results:
        raise ValueError(f"record-source missing for: {sorted(missing_results)}")

    # A batch is a snapshot. If a source changes again while the model is
    # working, publishing the older extraction would incorrectly mark the
    # newer bytes as ingested.
    for event in events:
        source_path = paths.root / event["path"]
        if event["kind"] == "deleted":
            if source_path.exists():
                raise RuntimeError(f"deleted source reappeared during batch; commit refused: {event['path']}")
            continue
        if not source_path.exists() or file_hash(source_path) != event["after"]["sha256"]:
            raise RuntimeError(f"source changed during batch; commit refused: {event['path']}")

    for rel, page in staged.items():
        unknown = set(page.source_ids) - active_ids
        if unknown:
            raise ValueError(f"{rel} references inactive/unknown source IDs: {sorted(unknown)}")
        expected_paths = {future[source_id]["path"] for source_id in page.source_ids}
        if set(page.sources) != expected_paths:
            raise ValueError(f"{rel} sources paths do not match source_ids")
        claims = parse_claims(page.path.read_text(encoding="utf-8"))
        claim_ids: set[str] = set()
        for claim in claims:
            if claim.claim_id in claim_ids:
                raise ValueError(f"{rel} has duplicate wiki-claim id: {claim.claim_id}")
            claim_ids.add(claim.claim_id)
            if not set(claim.source_ids) <= set(page.source_ids):
                raise ValueError(f"{rel} claim {claim.claim_id} references a source outside page provenance")
            known_chunks = {
                item["chunk_id"]
                for source_id in claim.source_ids
                for item in future[source_id].get("chunks", [])
            }
            if known_chunks and not set(claim.chunk_ids) <= known_chunks:
                raise ValueError(f"{rel} claim {claim.claim_id} references unknown chunks")

    # Exact title/alias identities are deterministic duplicate evidence. BM25
    # candidates remain advisory, but exact collisions must be merged through
    # checkout-page instead of silently creating a second page.
    identity_owner: dict[str, str] = {}
    for active_path in sorted(paths.wiki.rglob("*.md")):
        active_rel = active_path.relative_to(paths.wiki).as_posix()
        if active_rel in staged or active_rel in {"index.md", "log.md"}:
            continue
        try:
            active_page = parse_managed_page(active_path)
        except ValueError:
            continue
        if not active_page.managed:
            continue
        for value in (active_page.title, *active_page.aliases):
            identity = normalize_identity(value)
            if identity:
                identity_owner.setdefault(identity, active_rel)
    for rel, page in sorted(staged.items()):
        for value in (page.title, *page.aliases):
            identity = normalize_identity(value)
            owner = identity_owner.get(identity) if identity else None
            if owner and owner != rel:
                raise ValueError(f"duplicate page identity: {rel} conflicts with {owner} ({value})")
            if identity:
                identity_owner[identity] = rel

    for source_id, pages in result.get("source_pages", {}).items():
        for rel in pages:
            rel = _safe_page_rel(rel)
            page = staged.get(rel)
            if page is None or source_id not in page.source_ids:
                raise ValueError(f"recorded page {rel} is not staged with source {source_id}")

    obsolete: set[str] = set()
    for event in events:
        source_id = event["source_id"]
        old_pages = set(event.get("affected_pages", []))
        new_pages = set(result.get("source_pages", {}).get(source_id, []))
        if event["kind"] == "deleted":
            new_pages = set()
        for rel in old_pages:
            old_page_record = manifest.get("pages", {}).get(rel, {})
            survivors = set(old_page_record.get("source_ids", [])) & active_ids
            if rel in new_pages:
                if rel not in staged:
                    raise ValueError(f"affected page was not staged: {rel}")
            elif survivors:
                if rel not in staged or source_id in staged[rel].source_ids:
                    raise ValueError(f"shared page must be reconciled after source removal: {rel}")
            else:
                obsolete.add(rel)

    accidentally_restaged = obsolete & set(staged)
    if accidentally_restaged:
        raise ValueError(
            "obsolete pages must be removed from staging before commit: "
            f"{sorted(accidentally_restaged)}"
        )

    for rel, expected_hash in work.get("base_hashes", {}).items():
        active = paths.wiki / rel
        current_hash = file_hash(active) if active.exists() else None
        if current_hash != expected_hash:
            raise RuntimeError(f"active page changed during batch; commit refused: {rel}")
    for rel in set(staged) - set(work.get("base_hashes", {})):
        if (paths.wiki / rel).exists():
            raise RuntimeError(f"new staged page collides with an active page; commit refused: {rel}")
    for name, expected_hash in work.get("system_base_hashes", {}).items():
        active = paths.wiki / name
        current_hash = file_hash(active) if active.exists() else None
        if current_hash != expected_hash:
            raise RuntimeError(f"generated system page changed during batch; commit refused: {name}")
    return future, obsolete


def _archive(paths: ProjectPaths, batch_id: str, rel: str) -> bool:
    source = paths.wiki / rel
    if not source.exists():
        return False
    destination = paths.trash / batch_id / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return True


def _rebuild_index(paths: ProjectPaths) -> None:
    sections: dict[str, list[str]] = {}
    for path in sorted(paths.wiki.rglob("*.md")):
        rel = path.relative_to(paths.wiki).as_posix()
        if rel in {"index.md", "log.md"}:
            continue
        try:
            page = parse_managed_page(path)
        except ValueError:
            continue
        if not page.managed:
            continue
        tags = " ".join(f"#{item}" for item in page.category.split("/") if item)
        sections.setdefault(page.category.title(), []).append(
            f"- [[{rel[:-3]}|{page.title}]] — {page.summary} ( {tags})".rstrip()
        )
    lines = ["# Wiki Index", ""]
    for category, entries in sorted(sections.items()):
        lines.extend([f"## {category}", "", *entries, ""])
    paths.wiki.joinpath("index.md").write_text("\n".join(lines), encoding="utf-8")


def _append_log(paths: ProjectPaths, batch_id: str, events: list[dict[str, Any]], upserts: int, archived: int) -> None:
    path = paths.wiki / "log.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Wiki Log\n"
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
    detail = " ".join(f"{key}={value}" for key, value in sorted(kinds.items()))
    entry = f'- [{now_iso()}] SYNC batch="{batch_id}" {detail} pages_upserted={upserts} pages_archived={archived}\n'
    path.write_text(existing.rstrip() + "\n" + entry, encoding="utf-8")


def _rebuild_page_registry(paths: ProjectPaths, sources: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    for source in sources.values():
        source["pages"] = []
    for path in sorted(paths.wiki.rglob("*.md")):
        rel = path.relative_to(paths.wiki).as_posix()
        if rel in {"index.md", "log.md"}:
            continue
        try:
            page = parse_managed_page(path)
        except ValueError:
            continue
        if not page.managed:
            continue
        claims = parse_claims(path.read_text(encoding="utf-8"))
        pages[rel] = {
            "source_ids": list(page.source_ids),
            "content_sha256": file_hash(path),
            "updated_at": now_iso(),
            "identity": normalize_identity(page.title),
            "aliases": list(page.aliases),
            "claims": [
                {"id": claim.claim_id, "source_ids": list(claim.source_ids), "chunk_ids": list(claim.chunk_ids)}
                for claim in claims
            ],
        }
        for source_id in page.source_ids:
            if source_id in sources and sources[source_id].get("state") == "active":
                sources[source_id].setdefault("pages", []).append(rel)
    for source in sources.values():
        source["pages"] = sorted(set(source.get("pages", [])))
    return pages


def _recover_interrupted_commit(paths: ProjectPaths, work: dict[str, Any]) -> bool:
    batch_dir = (paths.root / work["result_path"]).parent
    journal_path = batch_dir / "commit-journal.json"
    journal = load_json(journal_path, {})
    if journal.get("phase") != "applying":
        return False
    backup_root = batch_dir / "backup-active" / "wiki"
    manifest_backup = batch_dir / "backup-active" / "manifest.json"
    for item in journal.get("targets", []):
        rel = _safe_page_rel(item["rel"]) if item["rel"] not in {"index.md", "log.md"} else item["rel"]
        active = paths.wiki / rel
        backup = backup_root / rel
        if item["existed"]:
            active.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, active)
        else:
            active.unlink(missing_ok=True)
    trash_batch = paths.trash / work["batch_id"]
    if trash_batch.exists() and trash_batch.resolve().parent == paths.trash.resolve():
        shutil.rmtree(trash_batch)
    if manifest_backup.exists():
        shutil.copy2(manifest_backup, paths.manifest)
    journal["phase"] = "recovered"
    journal["recovered_at"] = now_iso()
    atomic_json(journal_path, journal)
    return True


def _begin_commit_journal(paths: ProjectPaths, work: dict[str, Any], targets: set[str]) -> Path:
    batch_dir = (paths.root / work["result_path"]).parent
    backup_root = batch_dir / "backup-active" / "wiki"
    manifest_backup = batch_dir / "backup-active" / "manifest.json"
    manifest_backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.manifest, manifest_backup)
    records = []
    for rel in sorted(targets):
        active = paths.wiki / rel
        existed = active.exists()
        records.append({"rel": rel, "existed": existed})
        if existed:
            backup = backup_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(active, backup)
    journal_path = batch_dir / "commit-journal.json"
    atomic_json(journal_path, {
        "version": 1,
        "batch_id": work["batch_id"],
        "phase": "applying",
        "started_at": now_iso(),
        "targets": records,
    })
    return journal_path


def _finish_completed_commit(paths: ProjectPaths, work: dict[str, Any]) -> dict[str, Any] | None:
    journal_path = (paths.root / work["result_path"]).parent / "commit-journal.json"
    journal = load_json(journal_path, {})
    if journal.get("phase") != "complete":
        return None
    manifest = load_json(paths.manifest, empty_manifest())
    if manifest.get("last_batch") != work["batch_id"]:
        raise RuntimeError("complete commit journal does not match manifest; manual review required")
    result = journal.get("result") or {
        "status": "committed",
        "batch_id": work["batch_id"],
        "pages_upserted": 0,
        "pages_archived": 0,
    }
    paths.pending.unlink(missing_ok=True)
    return result


def commit(root: Path, batch_id: str) -> dict[str, Any]:
    paths = paths_for(root)
    with project_lock(paths):
        work = pending_work_order(root)
        if not work or work["batch_id"] != batch_id:
            raise RuntimeError(f"batch is not pending: {batch_id}")
        completed = _finish_completed_commit(paths, work)
        if completed is not None:
            return completed
        _recover_interrupted_commit(paths, work)
        manifest = load_json(paths.manifest, empty_manifest())
        result = load_json(paths.root / work["result_path"], {})
        staged = _scan_staged(work, paths)
        _validate_semantic_analysis(paths, work, staged)
        future, obsolete = _validate_batch(paths, manifest, work, result, staged)

        journal_path = _begin_commit_journal(paths, work, set(staged) | obsolete | {"index.md", "log.md"})
        try:
            archived = sum(1 for rel in sorted(obsolete) if _archive(paths, batch_id, rel))
            staging_root = paths.root / work["staging_wiki"]
            for rel in sorted(staged):
                source = staging_root / rel
                destination = paths.wiki / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(destination.suffix + ".incoming")
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)

            _rebuild_index(paths)
            _append_log(paths, batch_id, work["events"], len(staged), archived)
            pages = _rebuild_page_registry(paths, future)
            new_manifest = {
                "version": 3,
                "updated_at": now_iso(),
                "sources": future,
                "pages": pages,
                "last_batch": batch_id,
            }
            atomic_json(paths.manifest, new_manifest)
            committed_result = {
                "status": "committed",
                "batch_id": batch_id,
                "pages_upserted": len(staged),
                "pages_archived": archived,
            }
            journal = load_json(journal_path, {})
            journal.update({"phase": "complete", "completed_at": now_iso(), "result": committed_result})
            atomic_json(journal_path, journal)
            paths.pending.unlink(missing_ok=True)
            return committed_result
        except Exception:
            _recover_interrupted_commit(paths, work)
            raise


def restore_trash(root: Path, batch_id: str) -> dict[str, Any]:
    """Copy archived pages to a review area without reviving stale provenance."""
    paths = paths_for(root)
    source = paths.trash / batch_id
    if not source.exists() or source.resolve().parent != paths.trash.resolve():
        raise ValueError(f"trash batch does not exist: {batch_id}")
    destination = paths.state / "restored" / batch_id / "wiki"
    if destination.exists():
        raise ValueError(f"trash batch was already restored for review: {batch_id}")
    shutil.copytree(source, destination)
    return {
        "status": "restored_for_review",
        "batch_id": batch_id,
        "path": str(destination),
        "note": "Re-add the raw source and run sync before republishing these pages.",
    }


def abort(root: Path, batch_id: str) -> dict[str, Any]:
    paths = paths_for(root)
    with project_lock(paths):
        work = pending_work_order(root)
        if not work or work["batch_id"] != batch_id:
            raise RuntimeError(f"batch is not pending: {batch_id}")
        abandoned = paths.state / "abandoned" / batch_id
        abandoned.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str((paths.root / work["result_path"]).parent), str(abandoned))
        paths.pending.unlink(missing_ok=True)
        return {"status": "aborted", "batch_id": batch_id, "preserved_at": str(abandoned)}


def status(root: Path) -> dict[str, Any]:
    paths = initialize(root)
    manifest = load_json(paths.manifest, empty_manifest())
    pending = pending_work_order(root)
    config = load_config(paths.root)
    current = _source_snapshot(paths, config)
    events = _compute_events(
        manifest,
        current,
        int(config.get("semantic_revision", 1)),
    ) if pending is None else []
    return {
        "root": str(paths.root),
        "active_sources": sum(1 for item in manifest["sources"].values() if item.get("state") == "active"),
        "managed_pages": len(manifest.get("pages", {})),
        "pending_batch": pending["batch_id"] if pending else None,
        "unprepared_changes": len(events),
        "last_batch": manifest.get("last_batch"),
    }


def rewrite_moved_paths(root: Path, batch_id: str) -> int:
    """Convenience for path-only moves; exact replacements happen in staging."""
    paths = paths_for(root)
    work = pending_work_order(root)
    if not work or work["batch_id"] != batch_id:
        raise RuntimeError(f"batch is not pending: {batch_id}")
    staging = paths.root / work["staging_wiki"]
    changed = 0
    for event in work["events"]:
        if event["kind"] != "moved":
            continue
        for rel in event["affected_pages"]:
            page = staging / rel
            if not page.exists():
                continue
            original = page.read_text(encoding="utf-8")
            updated = replace_source_path(original, event["old_path"], event["path"])
            if updated != original:
                page.write_text(updated, encoding="utf-8")
                changed += 1
    return changed
