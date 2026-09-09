from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from wiki_common import ansi

from .core import (
    DEFAULT_CONFIG,
    abort,
    checkout_page,
    commit,
    initialize,
    load_config,
    paths_for,
    pending_work_order,
    prepare,
    preview_status,
    record_source,
    restore_trash,
    rewrite_moved_paths,
    status,
)
from .project import KnowledgeBaseError, resolve_kb_root

WIKI_CATEGORIES = ("concepts", "entities", "references", "skills", "sources", "synthesis")
EMBEDDING_KEYS = (
    "EMBEDDING_PROVIDER", "EMBEDDING_LOCAL_MODEL_PATH", "EMBEDDING_LOCAL_VARIANT",
    "EMBEDDING_LOCAL_THREADS", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL",
)
DEFAULT_EMBEDDING_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
RUNTIME_MODULES = ("mcp", "lancedb", "pyarrow", "httpx", "numpy", "onnxruntime", "transformers")


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _compact_chunk(chunk: dict[str, Any], include_preview: bool = False) -> dict[str, Any]:
    """Keep provenance coordinates while omitting the potentially huge text."""
    result = {
        key: chunk[key]
        for key in ("chunk_id", "sha256", "ordinal", "chars")
        if key in chunk
    }
    if include_preview and "preview" in chunk:
        result["preview"] = chunk["preview"]
    return result


def _compact_diff(diff: Any, include_chunk_ids: bool = False) -> Any:
    if not isinstance(diff, dict):
        return diff
    result = {
        key: diff[key]
        for key in (
            "added_count",
            "removed_count",
            "unchanged_count",
            "old_chars",
            "new_chars",
            "changed_chars",
            "changed_ratio",
        )
        if key in diff
    }
    if include_chunk_ids:
        for key in ("added", "removed", "unchanged"):
            if isinstance(diff.get(key), list):
                result[f"{key}_chunk_ids"] = [
                    item.get("chunk_id") for item in diff[key] if isinstance(item, dict)
                ]
    return result


def _compact_event(event: dict[str, Any], include_chunks: bool = False) -> dict[str, Any]:
    result = {
        key: event[key]
        for key in (
            "kind",
            "reason",
            "source_id",
            "path",
            "affected_pages",
            "semantic_revision",
            "extracted_path",
            "extraction_error",
        )
        if key in event
    }
    chunks = event.get("chunks", [])
    result["chunk_count"] = len(chunks) if isinstance(chunks, list) else 0
    result["diff"] = _compact_diff(event.get("diff"), include_chunk_ids=include_chunks)
    if include_chunks:
        for key in ("before", "after", "affected_claims", "identity_candidates"):
            if key in event:
                result[key] = event[key]
    if include_chunks and isinstance(chunks, list):
        result["chunks"] = [_compact_chunk(item) for item in chunks if isinstance(item, dict)]
    return result


def _pending_summary(work: dict[str, Any]) -> dict[str, Any]:
    return {
        key: work[key]
        for key in (
            "version",
            "batch_id",
            "created_at",
            "root",
            "wiki_dir",
            "staging_wiki",
            "semantic_revision",
            "analysis_path",
            "result_path",
            "instructions",
        )
        if key in work
    } | {"events": [_compact_event(event) for event in work.get("events", [])]}


def _pending_chunk(event: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """Return one bounded chunk with enough event context to preserve provenance."""
    chunks = event.get("chunks", [])
    chunk = next(
        (
            item
            for item in chunks
            if isinstance(item, dict) and item.get("ordinal") == ordinal
        ),
        None,
    )
    if chunk is None:
        raise ValueError(
            f"chunk ordinal {ordinal} is not present for source: {event.get('source_id', '<unknown>')}"
        )
    return {
        key: event[key]
        for key in ("kind", "reason", "source_id", "path", "extracted_path")
        if key in event
    } | {"chunk": chunk}


def _root(value: str) -> Path:
    return Path(value).expanduser().resolve()


SPLASH = r"""
 __  __   ___   ___  ___  _     ___ __      __  ___  ___  _  __
|  \/  | / _ \ | _ )|_ _|| |   | __|\ \    / / / _ \ | _ \| |/ /
| |\/| || (_) || _ \ | | | |__ | _|  \ \/\/ / | (_) ||   /| ' <
|_|  |_| \___/ |___/|___||____||___|  \_/\_/   \___/ |_|_\|_|\_\
"""


def _splash() -> None:
    try:
        version = importlib.metadata.version("mobilework")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    ansi.note(SPLASH.strip("\n"), ansi.CYAN)
    ansi.note(f"  mobilework {version} · wiki 构建维护引擎\n", ansi.DIM)


def _human_line(state: str, text: str) -> None:
    """Decorative echo of a machine-readable line; stdout JSON stays authoritative."""
    ansi.note(f"  {ansi.paint(state, ansi.status_color(state))}  {text}")


def _doctor_summary(report: dict[str, Any]) -> None:
    for check in report["checks"]:
        state = str(check["status"])
        ansi.note(f"  {ansi.paint(state.upper().ljust(4), ansi.status_color(state))}  {check['name']}: {check['detail']}")
    overall = str(report["status"])
    ansi.note(f"\n  {ansi.paint('overall: ' + overall, ansi.BOLD + ansi.status_color(overall))}")


def _maybe_reindex(root: Path) -> dict[str, Any]:
    """提交成功后重建检索索引。

    索引是派生产物：重建失败只上报、绝不回滚已落盘的事务。检索侧可能尚未实现，
    所以这里必须懒导入，导入失败等价于 skipped。
    """
    try:
        from wiki_retrieval import index as index_module
    except Exception as error:
        return {"status": "skipped", "error": f"{type(error).__name__}: {error}"}
    builder = getattr(index_module, "build", None)
    if builder is None:
        return {"status": "skipped", "error": "wiki_retrieval.index.build is not implemented yet"}
    try:
        builder(root)
    except Exception as error:
        return {"status": "failed", "error": f"{type(error).__name__}: {error}"}
    return {"status": "ok"}


def _check(name: str, state: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": state, "detail": detail}


def _env_values(root: Path) -> tuple[bool, dict[str, str]]:
    """读取 .env 中的 EMBEDDING_*，已导出的进程环境变量优先。"""
    path = root / ".env"
    values = {key: os.environ.get(key, "").strip() for key in EMBEDDING_KEYS}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, raw = stripped.partition("=")
            key = key.strip()
            if key in values and not values[key]:
                values[key] = raw.strip().strip("'\"")
    return path.exists(), values


def _check_interpreter() -> dict[str, str]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 11):
        return _check("interpreter", "fail", f"Python {version} < 3.11：prefix={sys.prefix}")
    if sys.prefix == sys.base_prefix:
        return _check("interpreter", "warn", f"Python {version} 未运行在虚拟环境中：prefix={sys.prefix}")
    return _check("interpreter", "ok", f"Python {version} prefix={sys.prefix}")


def _check_dependencies() -> dict[str, str]:
    missing = []
    for name in RUNTIME_MODULES:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    if missing:
        return _check("dependencies", "fail", f"缺少运行时依赖：{', '.join(missing)}（pip install -e \".[dev]\"）")
    return _check("dependencies", "ok", f"已就绪：{', '.join(RUNTIME_MODULES)}")


def _check_embedding_env(root: Path, values: dict[str, str], env_exists: bool) -> dict[str, str]:
    if not env_exists:
        return _check("embedding_env", "warn", f"缺少 {root / '.env'}")
    if (values["EMBEDDING_PROVIDER"] or "openrouter").lower() == "local":
        from wiki_retrieval import embedding

        embedding.load_dotenv(root)
        if not embedding.available():
            return _check("embedding_env", "fail", "本地 embedding 模型文件缺失")
        return _check("embedding_env", "ok", f"provider=local model={embedding.model_name()}")
    if not values["EMBEDDING_API_KEY"]:
        return _check("embedding_env", "warn", "未配置：EMBEDDING_API_KEY（向量通道将禁用）")
    model = values["EMBEDDING_MODEL"] or DEFAULT_EMBEDDING_MODEL
    base_url = values["EMBEDDING_BASE_URL"] or DEFAULT_EMBEDDING_BASE_URL
    return _check("embedding_env", "ok", f"model={model} base_url={base_url}")


def _check_layout(root: Path) -> tuple[dict[str, str], bool]:
    config_path = root / "wiki.config.json"
    if config_path.exists():
        try:
            json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            return _check("layout", "fail", f"wiki.config.json 无法解析：{error}"), False
        config = load_config(root)
        notes: list[str] = []
    else:
        config = {**DEFAULT_CONFIG, "watch": dict(DEFAULT_CONFIG["watch"])}
        notes = ["wiki.config.json 不存在（init 时生成，当前按默认配置检查）"]
    problems: list[str] = []
    sources = root / config["sources_dir"]
    if not sources.is_dir():
        problems.append(f"缺少 {config['sources_dir']}/")
        source_count = 0
    else:
        allowed = {item.lower() for item in config["include_extensions"]}
        source_count = sum(1 for path in sources.rglob("*") if path.is_file() and path.suffix.lower() in allowed)
        if source_count == 0:
            notes.append(f"{config['sources_dir']}/ 中没有可摄入的文本文件")
    wiki = root / config["wiki_dir"]
    if not wiki.is_dir():
        problems.append(f"缺少 {config['wiki_dir']}/")
    else:
        absent = [name for name in WIKI_CATEGORIES if not (wiki / name).is_dir()]
        if absent:
            problems.append(f"缺少分类目录：{', '.join(absent)}")
    if problems:
        return _check("layout", "fail", f"sources={source_count} " + "；".join(problems + notes)), True
    if notes:
        return _check("layout", "warn", f"sources={source_count} " + "；".join(notes)), True
    return _check("layout", "ok", f"sources={source_count} wiki/ 六个分类目录齐备"), True


def _check_manifest(root: Path) -> dict[str, str]:
    manifest_path = paths_for(root).manifest
    if not manifest_path.exists():
        return _check("manifest", "warn", "manifest.json 不存在（尚未 init）")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return _check("manifest", "fail", f"manifest.json 无法解析：{error}")
    sources = manifest.get("sources", {})
    dangling = sorted(
        f"{source_id}→{entry.get('path')}"
        for source_id, entry in sources.items()
        if entry.get("state") == "active" and not (root / str(entry.get("path", ""))).exists()
    )
    identities: dict[str, list[str]] = {}
    for rel, page in manifest.get("pages", {}).items():
        identities.setdefault(str(page.get("identity") or rel), []).append(rel)
    duplicates = {key: sorted(value) for key, value in identities.items() if len(value) > 1}
    problems: list[str] = []
    if dangling:
        problems.append(f"active source 指向的文件已不存在：{', '.join(dangling)}")
    if duplicates:
        problems.append("页面 identity 重复：" + "；".join(f"{key}={pages}" for key, pages in sorted(duplicates.items())))
    if problems:
        return _check("manifest", "fail", " | ".join(problems))
    active = sum(1 for entry in sources.values() if entry.get("state") == "active")
    return _check("manifest", "ok", f"version={manifest.get('version')} active_sources={active} pages={len(identities)}")


def _check_batch_residue(root: Path) -> dict[str, str]:
    paths = paths_for(root)
    notes: list[str] = []
    if paths.pending.exists():
        try:
            batch_id = json.loads(paths.pending.read_text(encoding="utf-8")).get("batch_id")
        except json.JSONDecodeError as error:
            return _check("batch_residue", "fail", f"pending.json 无法解析：{error}")
        notes.append(f"存在未完成的 batch：{batch_id}")
    residue: list[str] = []
    batches = paths.state / "batches"
    if batches.is_dir():
        for journal_path in sorted(batches.glob("*/commit-journal.json")):
            try:
                phase = json.loads(journal_path.read_text(encoding="utf-8")).get("phase")
            except json.JSONDecodeError:
                phase = "unparseable"
            if phase in {"applying", "unparseable"}:
                residue.append(f"{journal_path.parent.name}({phase})")
    if residue:
        notes.append("提交日志残留（再次 commit 会自动回滚恢复）：" + ", ".join(residue))
    if notes:
        return _check("batch_residue", "warn", " | ".join(notes))
    return _check("batch_residue", "ok", "无未完成 batch、无提交日志残留")


def _check_index_freshness(root: Path) -> dict[str, str]:
    try:
        from wiki_retrieval import index as index_module
    except Exception as error:
        return _check("index_freshness", "warn", f"检索侧不可用，索引状态未知：{type(error).__name__}: {error}")
    probe = getattr(index_module, "freshness", None)
    if probe is None:
        return _check("index_freshness", "warn", "wiki_retrieval.index.freshness 尚未实现")
    try:
        payload = probe(root)
    except Exception as error:
        return _check("index_freshness", "warn", f"freshness 调用失败：{type(error).__name__}: {error}")
    stale = payload.get("stale_index") if isinstance(payload, dict) else None
    return _check("index_freshness", "warn" if stale else "ok", json.dumps(payload, ensure_ascii=False, default=str))


def _check_embedding_probe(root: Path, values: dict[str, str]) -> dict[str, str]:
    if (values["EMBEDDING_PROVIDER"] or "openrouter").lower() == "local":
        try:
            from wiki_retrieval import embedding

            embedding.load_dotenv(root)
            vector, elapsed_ms = embedding.fetch("wiki doctor probe")
        except Exception as error:
            return _check("embedding_probe", "fail", f"本地推理失败：{type(error).__name__}: {error}")
        return _check(
            "embedding_probe", "ok",
            f"provider=local model={embedding.model_name()} dimension={len(vector)} elapsed_ms={elapsed_ms:.1f}",
        )
    base_url = values["EMBEDDING_BASE_URL"] or DEFAULT_EMBEDDING_BASE_URL
    api_key = values["EMBEDDING_API_KEY"]
    model = values["EMBEDDING_MODEL"] or DEFAULT_EMBEDDING_MODEL
    if not api_key:
        return _check("embedding_probe", "warn", "未配置 EMBEDDING_API_KEY，跳过连通性探测")
    try:
        import httpx

        response = httpx.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": "wiki doctor probe"},
            timeout=20.0,
        )
    except Exception as error:
        return _check("embedding_probe", "fail", f"请求失败：{type(error).__name__}: {error}")
    if response.status_code != 200:
        return _check("embedding_probe", "fail", f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        vector = response.json()["data"][0]["embedding"]
    except Exception as error:
        return _check("embedding_probe", "fail", f"响应格式异常：{type(error).__name__}: {error}")
    return _check("embedding_probe", "ok", f"model={model} dimension={len(vector)}")


def doctor(root: Path, probe: bool = False, env_root: Path | None = None) -> dict[str, Any]:
    """只读体检：不创建目录、不写状态文件。"""
    env_root = (env_root or root).resolve()
    env_exists, env_values = _env_values(env_root)
    layout, config_parseable = _check_layout(root)
    checks = [
        _check_interpreter(),
        _check_dependencies(),
        _check_embedding_env(env_root, env_values, env_exists),
        layout,
    ]
    if not config_parseable:
        checks.append(_check("manifest", "fail", "wiki.config.json 不可解析，跳过检查"))
        checks.append(_check("batch_residue", "fail", "wiki.config.json 不可解析，跳过检查"))
    else:
        checks.append(_check_manifest(root))
        checks.append(_check_batch_residue(root))
    checks.append(_check_index_freshness(root))
    if probe:
        checks.append(_check_embedding_probe(env_root, env_values))
    states = {item["status"] for item in checks}
    overall = "fail" if "fail" in states else "warn" if "warn" in states else "ok"
    return {"status": overall, "root": str(root), "checks": checks}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="wiki", description="Deterministic lifecycle engine for an OpenCode LLM Wiki")
    result.add_argument("--root", default=".", help="Wiki project root")
    kb_target = result.add_mutually_exclusive_group()
    kb_target.add_argument("--kb", help="Knowledge-base id from mobilework.config.json")
    kb_target.add_argument("--kb-root", help="Explicit knowledge-base root (must contain wiki.config.json)")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("preview", help=argparse.SUPPRESS)
    sub.add_parser("prepare")
    pending = sub.add_parser("pending")
    pending_view = pending.add_mutually_exclusive_group()
    pending_view.add_argument("--summary", action="store_true", help="Omit chunk bodies for a compact batch overview")
    pending_view.add_argument("--source-id", help="Show one event with compact chunk metadata but no chunk bodies")
    pending.add_argument("--chunk", type=int, metavar="ORDINAL", help="With --source-id, show exactly one bounded chunk body")
    record = sub.add_parser("record-source")
    record.add_argument("--batch", required=True)
    record.add_argument("--source-id", required=True)
    record.add_argument("--pages-json", required=True, help='JSON list, e.g. ["concepts/foo.md"]')
    checkout = sub.add_parser("checkout-page")
    checkout.add_argument("--batch", required=True)
    checkout.add_argument("--page", required=True)
    moved = sub.add_parser("rewrite-moves")
    moved.add_argument("--batch", required=True)
    finish = sub.add_parser("commit")
    finish.add_argument("--batch", required=True)
    finish.add_argument("--no-reindex", action="store_true", help="Skip the derived retrieval index rebuild")
    cancel = sub.add_parser("abort")
    cancel.add_argument("--batch", required=True)
    restore = sub.add_parser("restore-trash")
    restore.add_argument("--batch", required=True)
    legacy_sync = sub.add_parser("sync", help=argparse.SUPPRESS)
    legacy_sync.add_argument("--no-agent", action="store_true", help=argparse.SUPPRESS)
    legacy_watch = sub.add_parser("watch", help=argparse.SUPPRESS)
    legacy_watch.add_argument("--no-agent", action="store_true", help=argparse.SUPPRESS)
    legacy_watch.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    health = sub.add_parser("doctor")
    health.add_argument("--probe", action="store_true", help="Also probe embedding endpoint connectivity")
    return result


def main(argv: list[str] | None = None) -> int:
    # Windows often inherits a legacy GBK console encoding. Extracted Office
    # text may contain characters outside that code page (for example NBSP),
    # so make JSON output consistently UTF-8 instead of failing after prepare.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        _splash()
        parser().print_help(sys.stderr)
        return 0
    args = parser().parse_args(argv)
    app_root = _root(args.root)
    try:
        root = resolve_kb_root(
            app_root,
            kb=args.kb,
            kb_root=_root(args.kb_root) if args.kb_root else None,
        )
    except KnowledgeBaseError as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.command == "doctor":
        _splash()
    try:
        if args.command == "init":
            emit({"status": "initialized", "root": str(initialize(root).root)})
        elif args.command == "status":
            emit(status(root))
        elif args.command == "preview":
            emit(preview_status(root))
        elif args.command == "prepare":
            work = prepare(root)
            emit({"status": "unchanged"} if work is None else work)
        elif args.command == "pending":
            work = pending_work_order(root)
            if args.chunk is not None and not args.source_id:
                raise ValueError("--chunk requires --source-id")
            if work is None:
                emit({"status": "none"})
            elif args.summary:
                emit(_pending_summary(work))
            elif args.source_id:
                event = next(
                    (item for item in work.get("events", []) if item.get("source_id") == args.source_id),
                    None,
                )
                if event is None:
                    raise ValueError(f"source is not present in pending batch: {args.source_id}")
                emit(_pending_chunk(event, args.chunk) if args.chunk is not None else _compact_event(event, include_chunks=True))
            else:
                emit(work)
        elif args.command == "record-source":
            pages = json.loads(args.pages_json)
            if not isinstance(pages, list) or not all(isinstance(item, str) for item in pages):
                raise ValueError("--pages-json must be a JSON string array")
            record_source(root, args.batch, args.source_id, pages)
            emit({"status": "recorded", "source_id": args.source_id, "pages": pages})
        elif args.command == "checkout-page":
            emit(checkout_page(root, args.batch, args.page))
        elif args.command == "rewrite-moves":
            emit({"status": "rewritten", "pages": rewrite_moved_paths(root, args.batch)})
        elif args.command == "commit":
            outcome = commit(root, args.batch)
            outcome["reindex"] = {"status": "skipped", "error": "--no-reindex"} if args.no_reindex else _maybe_reindex(root)
            emit(outcome)
        elif args.command == "abort":
            emit(abort(root, args.batch))
        elif args.command == "restore-trash":
            emit(restore_trash(root, args.batch))
        elif args.command in {"sync", "watch"}:
            print(
                json.dumps(
                    {
                        "status": "migrated",
                        "error": f"wiki {args.command} 已停用；请运行 mobilework，然后在 TUI 中使用 /sync",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        elif args.command == "doctor":
            report = doctor(root, args.probe, app_root)
            emit(report)
            _doctor_summary(report)
            return 1 if report["status"] == "fail" else 0
        return 0
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"status": "interrupted", "pending": pending_work_order(root) is not None},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 130
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
