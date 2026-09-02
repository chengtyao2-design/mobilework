from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from .core import atomic_json, load_config


MIN_OPENCODE_VERSION = (1, 18, 25)
PROJECT_MARKERS = ("pyproject.toml", "wiki.config.json", ".opencode")
DEFAULT_MODELS = {
    "default": "openrouter/qwen/qwen3.8-flash",
    "primary": "openrouter/qwen/qwen3.8-max",
    "small": "openrouter/qwen/qwen3.7-flash",
}


class LaunchError(RuntimeError):
    pass


def find_project_root(start: Path) -> Path:
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if all((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    package_root = Path(__file__).resolve().parent.parent
    if all((package_root / marker).exists() for marker in PROJECT_MARKERS):
        return package_root
    raise LaunchError("找不到 Mobilework 项目根目录（需要 pyproject.toml、wiki.config.json 和 .opencode/）")


def project_python(root: Path) -> Path:
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise LaunchError("缺少项目虚拟环境；请先运行 python -m venv .venv 并安装项目依赖")


def check_python_environment(python: Path, root: Path) -> str:
    probe = (
        "import importlib.util,json,sys; "
        "names=('mcp','lancedb','pyarrow','httpx'); "
        "print(json.dumps({'version':list(sys.version_info[:3]),"
        "'missing':[n for n in names if importlib.util.find_spec(n) is None]}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", probe],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
        result = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise LaunchError(f"无法检查项目 Python 环境：{error}") from error
    version = tuple(int(value) for value in result.get("version", (0, 0, 0)))
    if completed.returncode != 0 or version < (3, 11, 0):
        rendered = ".".join(str(value) for value in version)
        raise LaunchError(f"项目需要 Python 3.11+，当前为 {rendered}")
    missing = [str(name) for name in result.get("missing", [])]
    if missing:
        raise LaunchError(
            "项目依赖缺失：" + "、".join(missing)
            + "；请运行 .\\.venv\\Scripts\\python.exe -m pip install -e ."
        )
    return ".".join(str(value) for value in version)


def find_opencode() -> str:
    override = os.environ.get("OPENCODE_BIN", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise LaunchError(f"OPENCODE_BIN 指向的文件不存在：{candidate}")
    names = ("opencode.cmd", "opencode.exe", "opencode") if os.name == "nt" else ("opencode",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise LaunchError("未找到 OpenCode CLI；请先安装 opencode-ai，并确认 opencode 在 PATH 中")


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", text)
    return tuple(int(value) for value in match.groups()) if match else None


def child_environment(root: Path, python: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["MOBILEWORK_ROOT"] = str(root)
    environment["MOBILEWORK_PYTHON"] = str(python)
    environment["WIKI_RETRIEVAL_PROJECT"] = str(root)
    environment["OPENCODE_CONFIG"] = str(root / ".wiki-state" / "opencode.runtime.json")
    # Keep CLI settings project-local. This also avoids Windows installations
    # where ~/.config/opencode was accidentally created as a file.
    environment["XDG_CONFIG_HOME"] = str(root / ".wiki-state" / "opencode-config")
    return environment


def check_opencode(executable: str, root: Path, environment: Mapping[str, str]) -> tuple[int, int, int]:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            cwd=root,
            env=dict(environment),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LaunchError(f"无法执行 OpenCode：{error}") from error
    output = "\n".join((completed.stdout, completed.stderr)).strip()
    version = _version_tuple(output)
    if completed.returncode != 0 or version is None:
        raise LaunchError(f"无法读取 OpenCode 版本：{output or '无输出'}")
    if version < MIN_OPENCODE_VERSION:
        required = ".".join(str(value) for value in MIN_OPENCODE_VERSION)
        actual = ".".join(str(value) for value in version)
        raise LaunchError(f"OpenCode {actual} 不支持 Mobilework TUI 插件；需要 {required} 或更高版本")
    return version


def validate_layout(root: Path) -> None:
    required = (
        root / "raw" / "sources",
        root / "wiki",
        root / ".opencode" / "agents" / "mobilework.md",
        root / ".opencode" / "agents" / "wiki-builder.md",
        root / ".opencode" / "plugins" / "mobilework" / "index.ts",
        root / ".opencode" / "plugins" / "mobilework" / "tui.ts",
        root / ".opencode" / "skills" / "wiki-retrieval-planner" / "SKILL.md",
    )
    missing = [path.relative_to(root).as_posix() for path in required if not path.exists()]
    if missing:
        raise LaunchError("项目布局不完整，缺少：" + "、".join(missing))


def model_profiles(root: Path) -> dict[str, str]:
    config = load_config(root)
    assistant = config.get("assistant", {})
    configured = assistant.get("models", {}) if isinstance(assistant, dict) else {}
    legacy = str(assistant.get("model", "")).strip() if isinstance(assistant, dict) else ""
    profiles = {
        name: str(configured.get(name, legacy if name == "default" and legacy else fallback)).strip()
        for name, fallback in DEFAULT_MODELS.items()
    }
    invalid = [name for name, model in profiles.items() if "/" not in model]
    if invalid:
        raise LaunchError(
            "wiki.config.json 模型配置无效："
            + "、".join(f"assistant.models.{name}" for name in invalid)
            + "（格式应为 provider/model）"
        )
    return profiles


def validate_model_auth(root: Path) -> str:
    profiles = model_profiles(root)
    model = profiles["default"]

    providers = {value.split("/", 1)[0].lower() for value in profiles.values()}
    provider = model.split("/", 1)[0].lower()
    env_keys = {
        "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_KEY"),
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
    }.get(provider, (f"{provider.upper().replace('-', '_')}_API_KEY",))
    if any(os.environ.get(name, "").strip() for name in env_keys) and providers == {provider}:
        return model

    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    auth_path = data_home / "opencode" / "auth.json"
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        auth = {}
    missing_providers = sorted(name for name in providers if name not in auth and not any(
        os.environ.get(key, "").strip()
        for key in ({
            "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_KEY"),
            "openai": ("OPENAI_API_KEY",),
            "anthropic": ("ANTHROPIC_API_KEY",),
        }.get(name, (f"{name.upper().replace('-', '_')}_API_KEY",)))
    ))
    if missing_providers:
        names = " / ".join(env_keys)
        raise LaunchError(
            f"模型配置缺少 {', '.join(missing_providers)} 凭据；请运行 opencode auth login，"
            f"或设置 {names}"
        )
    return model


def write_runtime_config(root: Path, python: Path) -> Path:
    path = root / ".wiki-state" / "opencode.runtime.json"
    plugin_uri = (root / ".opencode" / "plugins" / "mobilework").resolve().as_uri()
    models = model_profiles(root)
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "model": models["default"],
        "small_model": models["small"],
        "enabled_providers": sorted({value.split("/", 1)[0] for value in models.values()}),
        "plugin": [plugin_uri],
        "mcp": {
            "wiki-retrieval": {
                "type": "local",
                "command": [str(python), "-m", "wiki_retrieval.server"],
                "enabled": True,
                "environment": {"WIKI_RETRIEVAL_PROJECT": str(root)},
            }
        },
        "references": {
            "wiki": {
                "path": str((root / "wiki").resolve()),
                "description": "已构建的 Mobilework Wiki 页面",
            },
            "sources": {
                "path": str((root / "raw" / "sources").resolve()),
                "description": "Mobilework 原始资料（只读引用）",
            },
            "docs": {
                "path": str((root / "docs").resolve()),
                "description": "Mobilework 项目文档",
            },
        },
        "permission": {
            "read": {
                "*": "allow",
                "*.env": "deny",
                "*.env.*": "deny",
                "*.env.example": "allow",
                ".git/**": "deny",
                ".wiki-state/**": "deny",
                ".wiki-trash/**": "deny",
            }
        },
    }
    atomic_json(path, payload)
    atomic_json(
        root / ".wiki-state" / "opencode-config" / "opencode" / "cli.json",
        {
            "$schema": "https://opencode.ai/v2/cli.json",
            "plugins": [plugin_uri],
        },
    )
    # OpenCode 1.18.x still reads tui.json, while the v2 client uses cli.json.
    # Generate both from the same source so upgrades do not break the launcher.
    atomic_json(
        root / ".wiki-state" / "opencode-config" / "opencode" / "tui.json",
        {
            "$schema": "https://opencode.ai/tui.json",
            "plugin": [plugin_uri],
        },
    )
    return path


def build_command(executable: str, root: Path, profile: str = "default") -> list[str]:
    models = model_profiles(root)
    if profile not in ("default", "primary"):
        raise LaunchError("启动模型档位只能是 default 或 primary")
    model = models[profile]
    command = [executable, str(root), "--agent", "mobilework"]
    if model:
        command.extend(("--model", model))
    return command


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="mobilework", description="启动 Mobilework 对话式知识库")
    result.add_argument("--root", default=".", help="Mobilework 项目目录")
    result.add_argument("--check", action="store_true", help="只执行启动检查，不进入 TUI")
    result.add_argument(
        "--model-profile",
        choices=("default", "primary"),
        default="default",
        help="启动模型档位：default=Qwen Flash，primary=Qwen Max",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        root = find_project_root(Path(args.root))
        python = project_python(root)
        python_version = check_python_environment(python, root)
        validate_layout(root)
        validate_model_auth(root)
        models = model_profiles(root)
        model = models[args.model_profile]
        executable = find_opencode()
        environment = child_environment(root, python)
        write_runtime_config(root, python)
        version = check_opencode(executable, root, environment)
        if args.check:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "root": str(root),
                        "python": str(python),
                        "python_version": python_version,
                        "opencode": executable,
                        "opencode_version": ".".join(str(value) for value in version),
                        "model": model,
                        "models": models,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        return subprocess.call(build_command(executable, root, args.model_profile), cwd=root, env=environment)
    except LaunchError as error:
        print(f"mobilework: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
