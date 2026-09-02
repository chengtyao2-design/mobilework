from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiki_maintainer.cli import main as wiki_main
from wiki_maintainer.core import preview_status
from wiki_maintainer.launcher import (
    LaunchError,
    _version_tuple,
    build_command,
    check_python_environment,
    validate_model_auth,
    write_runtime_config,
)


def _write_config(root: Path, model: str = "openrouter/qwen/qwen3.8-flash") -> None:
    (root / "wiki.config.json").write_text(
        json.dumps({"assistant": {"model": model}}),
        encoding="utf-8",
    )


def test_preview_is_read_only_and_reports_new_source(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "raw" / "sources").mkdir(parents=True)
    (root / "wiki").mkdir()
    (root / "raw" / "sources" / "example.md").write_text("# Example\n", encoding="utf-8")
    _write_config(root)

    result = preview_status(root)

    assert result["change_total"] == 1
    assert result["change_counts"]["new"] == 1
    assert not (root / ".wiki-state").exists()


@pytest.mark.parametrize("arguments", [["sync"], ["sync", "--no-agent"], ["watch"], ["watch", "--once"]])
def test_legacy_sync_commands_only_show_migration(arguments: list[str], tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    result = wiki_main(["--root", str(root), *arguments])

    assert result == 2
    assert "mobilework" in capsys.readouterr().err
    assert not root.exists()


def test_launcher_version_parser() -> None:
    assert _version_tuple("opencode 1.18.26") == (1, 18, 26)
    assert _version_tuple("unknown") is None


def test_python_dependency_check_reports_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "wiki_maintainer.launcher.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"version": [3, 13, 0], "missing": ["lancedb"]}),
        ),
    )

    with pytest.raises(LaunchError, match="lancedb"):
        check_python_environment(tmp_path / "python.exe", tmp_path)


def test_model_auth_accepts_environment_key(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")

    assert validate_model_auth(tmp_path) == "openrouter/qwen/qwen3.8-flash"


def test_model_auth_has_actionable_error(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-data"))

    with pytest.raises(LaunchError, match="opencode auth login"):
        validate_model_auth(tmp_path)


def test_runtime_config_registers_references_and_tui_compatibility(tmp_path: Path) -> None:
    root = tmp_path / "project"
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (root / ".opencode" / "plugins" / "mobilework").mkdir(parents=True)

    runtime_path = write_runtime_config(root, python)
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    cli = json.loads((root / ".wiki-state/opencode-config/opencode/cli.json").read_text(encoding="utf-8"))
    tui = json.loads((root / ".wiki-state/opencode-config/opencode/tui.json").read_text(encoding="utf-8"))

    assert set(runtime["references"]) == {"wiki", "sources", "docs"}
    assert runtime["permission"]["read"][".wiki-state/**"] == "deny"
    assert cli["plugins"] == runtime["plugin"]
    assert tui["plugin"] == runtime["plugin"]


def test_launcher_starts_one_fresh_tui_without_nested_run(tmp_path: Path) -> None:
    _write_config(tmp_path)

    command = build_command("opencode.cmd", tmp_path)

    assert command[:2] == ["opencode.cmd", str(tmp_path)]
    assert "run" not in command
    assert "--continue" not in command
    assert command[-2:] == ["--model", "openrouter/qwen/qwen3.8-flash"]


def test_plugin_autoload_order_and_tier_boundaries(repo_root: Path) -> None:
    server = (repo_root / ".opencode/plugins/mobilework/index.ts").read_text(encoding="utf-8")
    tui = (repo_root / ".opencode/plugins/mobilework/tui.ts").read_text(encoding="utf-8")

    assert '["wiki-retrieval-planner", `wiki-ask-${tier}`]' in server
    assert server.index("wiki-retrieval-planner") < server.index("`wiki-ask-${tier}`")
    assert "tier === \"medium\" || tier === \"high\"" in server
    assert "Never change or silently escalate it" in server
    assert 'slash: { name: "retrieve"' in tui
    assert "DialogSelect<Tier>" in tui
    assert 'slash: { name: "sync"' in tui
    assert "promptAsync" in tui
    assert "opencode run" not in tui


def test_skill_tier_limits_and_planner_contract(repo_root: Path) -> None:
    planner = (repo_root / ".opencode/skills/wiki-retrieval-planner/SKILL.md").read_text(encoding="utf-8")
    medium = (repo_root / ".opencode/skills/wiki-ask-medium/SKILL.md").read_text(encoding="utf-8")
    high = (repo_root / ".opencode/skills/wiki-ask-high/SKILL.md").read_text(encoding="utf-8")

    assert "Do not call tools and do not answer the question" in planner
    assert "Never change it or silently escalate" in planner
    assert "at most two rounds" in medium
    assert "after five rounds" in high
    assert "duplicate:true" in high
