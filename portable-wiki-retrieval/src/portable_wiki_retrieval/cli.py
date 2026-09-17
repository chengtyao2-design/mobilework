from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import __version__, config as config_module
from .errors import RetrievalError
from .service import RetrievalService


def _emit(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str), file=stream)


def _service(args: argparse.Namespace) -> RetrievalService:
    return RetrievalService(args.config)


def _doctor(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append({"name": "python", "ok": sys.version_info >= (3, 11), "value": sys.version.split()[0]})
    try:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
        connection.close()
        fts5 = True
    except sqlite3.Error:
        fts5 = False
    checks.append({"name": "sqlite_fts5", "ok": fts5, "value": sqlite3.sqlite_version})
    service = _service(args)
    checks.append({"name": "config", "ok": True, "path": str(service.config_path)})
    checks.append({"name": "openrouter_key", "ok": service.client.embedding_available(),
                   "value": "configured" if service.client.embedding_available() else "missing"})
    for client in ("opencode", "codex", "claude"):
        checks.append({"name": f"client_{client}", "ok": shutil.which(client) is not None,
                       "value": shutil.which(client)})
    return {"version": __version__, "ok": all(item["ok"] for item in checks[:3]),
            "checks": checks, "state_dir": str(service.state_dir),
            "registered_wikis": len(service.registry.list())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portable-wiki", description="本地 Markdown Wiki 检索服务")
    parser.add_argument("--config", help="显式配置文件路径")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查运行环境")
    commands.add_parser("serve", help="启动 stdio MCP 服务")

    config = commands.add_parser("config", help="查看和修改配置")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("path")
    config_commands.add_parser("show")
    config_commands.add_parser("validate")
    get = config_commands.add_parser("get")
    get.add_argument("key")
    set_command = config_commands.add_parser("set")
    set_command.add_argument("key")
    set_command.add_argument("value")
    unset = config_commands.add_parser("unset")
    unset.add_argument("key")

    wiki = commands.add_parser("wiki", help="注册和维护 Wiki 索引")
    wiki_commands = wiki.add_subparsers(dest="wiki_command", required=True)
    inspect = wiki_commands.add_parser("inspect")
    inspect.add_argument("root")
    inspect.add_argument("--wiki-id", default="inspection")
    register = wiki_commands.add_parser("register")
    register.add_argument("wiki_id")
    register.add_argument("root")
    register.add_argument("--name")
    register.add_argument("--include", action="append")
    register.add_argument("--exclude", action="append")
    wiki_commands.add_parser("list")
    update = wiki_commands.add_parser("update")
    update.add_argument("wiki_id")
    update.add_argument("--root")
    update.add_argument("--name")
    update.add_argument("--enable", action=argparse.BooleanOptionalAction)
    unregister = wiki_commands.add_parser("unregister")
    unregister.add_argument("wiki_id")
    reindex = wiki_commands.add_parser("reindex")
    reindex.add_argument("wiki_id")
    return parser


def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "serve":
        if args.config:
            os.environ["PORTABLE_WIKI_CONFIG"] = str(Path(args.config).expanduser().resolve())
        from .server import main as serve
        serve()
        return None
    service = _service(args)
    if args.command == "config":
        if args.config_command == "path":
            return {"path": str(service.config_path), "exists": service.config_path.is_file()}
        if args.config_command == "show":
            return service.get_settings()
        if args.config_command == "validate":
            config_module.validate(service.config)
            return {"valid": True, "path": str(service.config_path)}
        if args.config_command == "get":
            return {"key": args.key, "value": config_module.get_dotted(service.config, args.key)}
        if args.config_command == "set":
            return service.update_settings("global", {args.key: config_module.parse_cli_value(args.value)})
        if args.config_command == "unset":
            return service.reset_settings("global", [args.key])
    if args.command == "wiki":
        if args.wiki_command == "inspect":
            return service.inspect_wiki(args.root, args.wiki_id)
        if args.wiki_command == "register":
            return service.register_wiki(args.wiki_id, args.root, name=args.name,
                                         include=args.include, exclude=args.exclude)
        if args.wiki_command == "list":
            return service.list_wikis()
        if args.wiki_command == "update":
            changes = {key: value for key, value in {"root": args.root, "name": args.name,
                                                      "enabled": args.enable}.items() if value is not None}
            return service.update_wiki(args.wiki_id, changes)
        if args.wiki_command == "unregister":
            return service.unregister_wiki(args.wiki_id)
        if args.wiki_command == "reindex":
            return service.reindex(args.wiki_id)
    raise RetrievalError("INVALID_ARGUMENT", "无法识别命令")


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = dispatch(args)
        if result is not None:
            _emit(result)
    except RetrievalError as error:
        _emit({"ok": False, "error": error.as_dict()}, stream=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
