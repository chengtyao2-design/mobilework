from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
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
    record_source,
    restore_trash,
    rewrite_moved_paths,
    status,
)

WIKI_CATEGORIES = ("concepts", "entities", "references", "skills", "sources", "synthesis")
EMBEDDING_KEYS = ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL")
DEFAULT_EMBEDDING_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
RUNTIME_MODULES = ("mcp", "lancedb", "pyarrow", "httpx")


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


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


def run_agent(root: Path, batch_id: str) -> int:
    config = load_config(root)
    prompt = (
        "Use the wiki-maintainer skill to process the existing pending batch "
        f"{batch_id}. Follow its staging and commit protocol. Do not ask for confirmation."
    )
    command = [str(part).replace("{root}", str(root)).replace("{prompt}", prompt) for part in config["watch"]["agent_command"]]
    executable = shutil.which(command[0])
    if not executable:
        raise RuntimeError(f"OpenCode executable not found: {command[0]}")
    command[0] = executable
    return subprocess.run(command, cwd=root, check=False).returncode


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


def sync_once(root: Path, invoke_agent: bool) -> dict[str, Any]:
    work = prepare(root)
    if work is None:
        return {"status": "unchanged"}
    if not invoke_agent:
        return {"status": "prepared", "batch_id": work["batch_id"], "work_order": work}
    code = run_agent(root, work["batch_id"])
    current = pending_work_order(root)
    return {
        "status": "committed" if current is None and code == 0 else "agent_failed_or_incomplete",
        "batch_id": work["batch_id"],
        "agent_exit_code": code,
        "pending": current is not None,
    }


def watch(root: Path, invoke_agent: bool, once: bool = False) -> int:
    config = load_config(root)
    interval = float(config["watch"]["interval_seconds"])
    settle = float(config["watch"]["settle_seconds"])
    retry = float(config["watch"]["retry_seconds"])
    last_signature: str | None = None
    stable_since = time.monotonic()
    last_agent_attempt = 0.0
    print(f"Watching {root / config['sources_dir']} (Ctrl+C to stop)", file=sys.stderr, flush=True)
    while True:
        snapshot = status(root)
        signature = json.dumps(
            {"changes": snapshot["unprepared_changes"], "pending": snapshot["pending_batch"]},
            sort_keys=True,
        )
        if signature != last_signature:
            last_signature = signature
            stable_since = time.monotonic()
        elif snapshot["pending_batch"] and invoke_agent and time.monotonic() - last_agent_attempt >= retry:
            code = run_agent(root, snapshot["pending_batch"])
            last_agent_attempt = time.monotonic()
            remaining = pending_work_order(root)
            result = {
                "status": "committed" if code == 0 and remaining is None else "agent_failed_or_incomplete",
                "batch_id": snapshot["pending_batch"],
                "agent_exit_code": code,
                "pending": remaining is not None,
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
            _human_line(str(result["status"]), f"batch {result['batch_id']} agent_exit={code}")
            if once:
                return 0 if result["status"] == "committed" else 3
        elif snapshot["pending_batch"] and not invoke_agent and once:
            return 0
        elif snapshot["pending_batch"] is None and snapshot["unprepared_changes"] and time.monotonic() - stable_since >= settle:
            result = sync_once(root, invoke_agent)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            _human_line(str(result["status"]), f"batch {result.get('batch_id', '-')}")
            stable_since = time.monotonic()
            if once:
                return 0 if result["status"] in {"prepared", "committed"} else 1
        elif once and not snapshot["unprepared_changes"]:
            return 0
        time.sleep(interval)


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


def _check_embedding_probe(values: dict[str, str]) -> dict[str, str]:
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


def doctor(root: Path, probe: bool = False) -> dict[str, Any]:
    """只读体检：不创建目录、不写状态文件。"""
    env_exists, env_values = _env_values(root)
    layout, config_parseable = _check_layout(root)
    checks = [_check_interpreter(), _check_dependencies(), _check_embedding_env(root, env_values, env_exists), layout]
    if not config_parseable:
        checks.append(_check("manifest", "fail", "wiki.config.json 不可解析，跳过检查"))
        checks.append(_check("batch_residue", "fail", "wiki.config.json 不可解析，跳过检查"))
    else:
        checks.append(_check_manifest(root))
        checks.append(_check_batch_residue(root))
    checks.append(_check_index_freshness(root))
    if probe:
        checks.append(_check_embedding_probe(env_values))
    states = {item["status"] for item in checks}
    overall = "fail" if "fail" in states else "warn" if "warn" in states else "ok"
    return {"status": overall, "root": str(root), "checks": checks}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="wiki", description="Deterministic lifecycle engine for an OpenCode LLM Wiki")
    result.add_argument("--root", default=".", help="Wiki project root")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("prepare")
    pending = sub.add_parser("pending")
    pending.set_defaults(_unused=True)
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
    sync = sub.add_parser("sync")
    sync.add_argument("--no-agent", action="store_true")
    watcher = sub.add_parser("watch")
    watcher.add_argument("--no-agent", action="store_true")
    watcher.add_argument("--once", action="store_true")
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
    root = _root(args.root)
    if args.command in {"doctor", "watch"}:
        _splash()
    try:
        if args.command == "init":
            emit({"status": "initialized", "root": str(initialize(root).root)})
        elif args.command == "status":
            emit(status(root))
        elif args.command == "prepare":
            work = prepare(root)
            emit({"status": "unchanged"} if work is None else work)
        elif args.command == "pending":
            emit(pending_work_order(root) or {"status": "none"})
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
        elif args.command == "sync":
            outcome = sync_once(root, not args.no_agent)
            emit(outcome)
            if outcome["status"] == "agent_failed_or_incomplete":
                return 3
        elif args.command == "watch":
            return watch(root, not args.no_agent, args.once)
        elif args.command == "doctor":
            report = doctor(root, args.probe)
            emit(report)
            _doctor_summary(report)
            return 1 if report["status"] == "fail" else 0
        return 0
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
