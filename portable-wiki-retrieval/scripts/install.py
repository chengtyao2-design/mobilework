from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def copy_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def check_environment() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("需要 Python 3.11 或更高版本")
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
    except sqlite3.Error as error:
        raise SystemExit(f"当前 Python 的 SQLite 不支持 FTS5：{error}") from error
    finally:
        connection.close()


def venv_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_venv(skip_dependencies: bool) -> Path:
    directory = ROOT / ".venv"
    python = venv_python(directory)
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(directory)
    if not skip_dependencies:
        subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.lock")], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(ROOT)], check=True)
    return python.resolve()


def ensure_local_config() -> Path:
    config = ROOT / "config.toml"
    if not config.exists():
        shutil.copy2(ROOT / "config.example.toml", config)
    env = ROOT / ".env"
    if not env.exists():
        shutil.copy2(ROOT / ".env.example", env)
    return config.resolve()


def mcp_payload(python: Path, config: Path) -> dict:
    return {
        "type": "local",
        "command": [str(python), "-m", "portable_wiki_retrieval.server"],
        "enabled": True,
        "environment": {
            "PORTABLE_WIKI_CONFIG": str(config),
            "OPENROUTER_API_KEY": "{env:OPENROUTER_API_KEY}",
        },
    }


def opencode_v2() -> bool:
    executable = shutil.which("opencode")
    if not executable:
        return False
    try:
        completed = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
        version = (completed.stdout or completed.stderr).strip().splitlines()[0]
        return version.lstrip("v").split(".", 1)[0] == "2"
    except (OSError, subprocess.SubprocessError, IndexError):
        return False


def install_opencode(project: Path, python: Path, config: Path) -> list[Path]:
    copy_tree(ROOT / "skills/wiki-retrieval", project / ".opencode/skills/wiki-retrieval")
    copy_tree(ROOT / "skills/wiki-retrieval", project / ".agents/skills/wiki-retrieval")
    target = project / "opencode.json"
    data: dict = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            snippet = project / "portable-wiki.opencode.snippet.json"
            snippet.write_text(json.dumps({"mcp": {"portable-wiki": mcp_payload(python, config)}},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"现有 opencode 配置含注释，未自动改写；请合并：{snippet}")
            return [snippet]
    payload = mcp_payload(python, config)
    if opencode_v2():
        payload.pop("enabled", None)
        payload["disabled"] = False
        data.setdefault("mcp", {}).setdefault("servers", {})["portable-wiki"] = payload
    else:
        data.setdefault("mcp", {})["portable-wiki"] = payload
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [target, project / ".opencode/skills/wiki-retrieval/SKILL.md"]


def install_codex(project: Path, python: Path, config: Path) -> list[Path]:
    copy_tree(ROOT / "skills/wiki-retrieval", project / ".agents/skills/wiki-retrieval")
    target = project / ".codex/config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if "[mcp_servers.portable-wiki]" not in existing:
        block = (f'\n[mcp_servers.portable-wiki]\ncommand = {json.dumps(str(python))}\n'
                 f'args = ["-m", "portable_wiki_retrieval.server"]\n'
                 f'env = {{ PORTABLE_WIKI_CONFIG = {json.dumps(str(config))} }}\n')
        target.write_text(existing.rstrip() + block, encoding="utf-8")
    return [target, project / ".agents/skills/wiki-retrieval/SKILL.md"]


def install_claude(project: Path, python: Path, config: Path) -> list[Path]:
    copy_tree(ROOT / "skills/wiki-retrieval", project / ".claude/skills/wiki-retrieval")
    target = project / ".mcp.json"
    data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    data.setdefault("mcpServers", {})["portable-wiki"] = {
        "command": str(python), "args": ["-m", "portable_wiki_retrieval.server"],
        "env": {"PORTABLE_WIKI_CONFIG": str(config)},
    }
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [target, project / ".claude/skills/wiki-retrieval/SKILL.md"]


def main() -> None:
    parser = argparse.ArgumentParser(description="安装 Portable Wiki Retrieval")
    parser.add_argument("--client", choices=["opencode", "codex", "claude-code", "all"], default="all")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--skip-dependencies", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    check_environment()
    python = ensure_venv(args.skip_dependencies)
    config = ensure_local_config()
    project = args.project.expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    selected = ["opencode", "codex", "claude-code"] if args.client == "all" else [args.client]
    outputs: list[Path] = []
    if "opencode" in selected:
        outputs.extend(install_opencode(project, python, config))
    if "codex" in selected:
        outputs.extend(install_codex(project, python, config))
    if "claude-code" in selected:
        outputs.extend(install_claude(project, python, config))
    print("安装完成。请在 .env 中填写唯一的 OPENROUTER_API_KEY。")
    print(f"Python: {python}\n配置: {config}\n状态目录: 运行 portable-wiki doctor 查看")
    for output in outputs:
        print(f"已写入: {output}")


if __name__ == "__main__":
    main()
