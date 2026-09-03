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
    child_environment,
    check_python_environment,
    model_profiles,
    validate_model_auth,
    write_runtime_config,
)
from wiki_maintainer.project import KnowledgeBaseError, resolve_kb_root


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


def test_model_profiles_support_default_primary_and_small(tmp_path: Path) -> None:
    (tmp_path / "wiki.config.json").write_text(
        json.dumps(
            {
                "assistant": {
                    "models": {
                        "default": "openrouter/qwen/qwen3.8-flash",
                        "primary": "openrouter/qwen/qwen3.8-max",
                        "small": "openrouter/qwen/qwen3.7-flash",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert model_profiles(tmp_path) == {
        "default": "openrouter/qwen/qwen3.8-flash",
        "primary": "openrouter/qwen/qwen3.8-max",
        "small": "openrouter/qwen/qwen3.7-flash",
    }


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
    cli = json.loads((root / ".mobilework-state/opencode-config/opencode/cli.json").read_text(encoding="utf-8"))
    tui = json.loads((root / ".mobilework-state/opencode-config/opencode/tui.json").read_text(encoding="utf-8"))

    assert set(runtime["references"]) == {"wiki", "sources", "docs"}
    assert runtime["permission"]["read"][".wiki-state/**"] == "deny"
    assert runtime["permission"]["read"][".mobilework-state/**"] == "deny"
    assert runtime["model"] == "openrouter/qwen/qwen3.8-flash"
    assert runtime["small_model"] == "openrouter/qwen/qwen3.7-flash"
    assert runtime["enabled_providers"] == ["openrouter"]
    assert runtime["provider"]["openrouter"]["options"] == {
        "timeout": 90_000,
        "chunkTimeout": 45_000,
    }
    assert runtime["tool_output"] == {"max_lines": 400, "max_bytes": 16000}
    assert cli["plugins"] == runtime["plugin"]
    assert tui["plugin"] == runtime["plugin"]


def test_resolve_kb_root_uses_default_and_explicit_selection(tmp_path: Path) -> None:
    app = tmp_path / "app"
    for kb_id in ("kb_a", "kb_b"):
        kb_root = app / "kb" / kb_id
        kb_root.mkdir(parents=True)
        (kb_root / "wiki.config.json").write_text("{}", encoding="utf-8")
    (app / "mobilework.config.json").write_text(
        json.dumps(
            {
                "default_kb": "kb_a",
                "knowledge_bases": {
                    "kb_a": {"path": "kb/kb_a"},
                    "kb_b": {"path": "kb/kb_b"},
                },
            }
        ),
        encoding="utf-8",
    )

    assert resolve_kb_root(app) == (app / "kb" / "kb_a").resolve()
    assert resolve_kb_root(app, kb="kb_b") == (app / "kb" / "kb_b").resolve()
    assert resolve_kb_root(app, kb_root=app / "kb" / "kb_b") == (app / "kb" / "kb_b").resolve()


def test_resolve_kb_root_rejects_unknown_or_escaping_kb(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "mobilework.config.json").write_text(
        json.dumps(
            {
                "default_kb": "outside",
                "knowledge_bases": {"outside": {"path": "../outside"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeBaseError, match="escapes"):
        resolve_kb_root(app)


def test_wiki_cli_targets_selected_kb(tmp_path: Path, capsys) -> None:
    app = tmp_path / "app"
    kb_root = app / "kb" / "kb_a"
    kb_root.mkdir(parents=True)
    (kb_root / "wiki.config.json").write_text("{}", encoding="utf-8")
    (app / "mobilework.config.json").write_text(
        json.dumps(
            {
                "default_kb": "kb_a",
                "knowledge_bases": {"kb_a": {"path": "kb/kb_a"}},
            }
        ),
        encoding="utf-8",
    )

    assert wiki_main(["--root", str(app), "--kb", "kb_a", "init"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["root"]) == kb_root.resolve()
    assert (kb_root / "raw" / "sources").is_dir()
    assert not (app / ".wiki-state").exists()


def test_runtime_config_keeps_app_state_separate_from_selected_kb(tmp_path: Path) -> None:
    app = tmp_path / "app"
    kb_root = app / "kb" / "kb_a"
    python = app / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (app / ".opencode" / "plugins" / "mobilework").mkdir(parents=True)
    kb_root.mkdir(parents=True)
    _write_config(kb_root)
    (app / "mobilework.config.json").write_text(
        json.dumps(
            {
                "default_kb": "kb_a",
                "knowledge_bases": {"kb_a": {"path": "kb/kb_a"}},
            }
        ),
        encoding="utf-8",
    )

    runtime_path = write_runtime_config(app, python, kb_root)
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    environment = child_environment(app, python, kb_root)

    assert runtime_path == app / ".mobilework-state" / "opencode.runtime.json"
    assert Path(runtime["references"]["wiki"]["path"]) == (kb_root / "wiki").resolve()
    assert Path(runtime["references"]["sources"]["path"]) == (kb_root / "raw" / "sources").resolve()
    assert environment["MOBILEWORK_ROOT"] == str(app)
    assert environment["MOBILEWORK_KB"] == "kb_a"
    assert environment["MOBILEWORK_KB_ROOT"] == str(kb_root.resolve())
    assert environment["OPENCODE_CONFIG"] == str(runtime_path)


def test_launcher_starts_one_fresh_tui_without_nested_run(tmp_path: Path) -> None:
    _write_config(tmp_path)

    command = build_command("opencode.cmd", tmp_path)

    assert command[:2] == ["opencode.cmd", str(tmp_path)]
    assert "run" not in command
    assert "--continue" not in command
    assert command[-2:] == ["--model", "openrouter/qwen/qwen3.8-flash"]

    primary = build_command("opencode.cmd", tmp_path, "primary")
    assert primary[-2:] == ["--model", "openrouter/qwen/qwen3.8-max"]


def test_plugin_autoload_order_and_tier_boundaries(repo_root: Path) -> None:
    server = (repo_root / ".opencode/plugins/mobilework/index.ts").read_text(encoding="utf-8")
    tui = (repo_root / ".opencode/plugins/mobilework/tui.ts").read_text(encoding="utf-8")

    assert '["wiki-retrieval-planner", "wiki-ask-federated"]' in server
    assert "Never change or silently escalate it" in server
    assert "sessionTier" not in server
    assert "resolveConfig(readPreferences(root)" in server
    assert "RetrievalGuard" in server
    assert "output.args.overrides" in server
    assert 'input.tool !== "skill"' in server
    assert "MANAGED_RETRIEVAL_SKILLS" in server
    assert 'output.title = FRIENDLY_TOOL_TITLES[kind]' in server
    assert "compactToolOutput" not in server
    assert 'slash: { name: "retrieve"' in tui
    assert "DialogSelect<Tier>" in tui
    assert 'slash: { name: "sync"' in tui
    assert '"--root", root, "--kb-root", kbRoot' in tui
    assert "promptAsync" in tui
    assert "opencode run" not in tui
    assert 'api.event.on("session.status"' in tui
    assert "45_000" in tui
    assert "90_000" in tui
    assert "if (waitTimers.has(sessionID)) return" in tui
    assert "api.client.session.abort" in tui
    assert "MOBILEWORK_RETRY_ANSWER_ONLY" in tui
    assert "不要再次检索" in tui
    assert "MOBILEWORK_RETRY_ANSWER_ONLY" in server
    assert "Timeout recovery must reuse" in server
    builder = (repo_root / ".opencode/agents/wiki-builder.md").read_text(encoding="utf-8")
    assert "--root <应用根> --kb-root <MOBILEWORK_KB_ROOT>" in builder


def test_skill_tier_limits_and_planner_contract(repo_root: Path) -> None:
    planner = (repo_root / ".opencode/skills/wiki-retrieval-planner/SKILL.md").read_text(encoding="utf-8")
    medium = (repo_root / ".opencode/skills/wiki-ask-medium/SKILL.md").read_text(encoding="utf-8")
    high = (repo_root / ".opencode/skills/wiki-ask-high/SKILL.md").read_text(encoding="utf-8")

    assert "Do not call tools and do not answer the question" in planner
    assert "Never change it or silently escalate" in planner
    unified = (repo_root / ".opencode/skills/wiki-ask-federated/SKILL.md").read_text(encoding="utf-8")
    assert "route_knowledge_bases" in unified
    assert "max_tool_calls" in unified
    assert "duplicate:true" in unified
    for content in (unified,):
        assert "human-readable document titles" in content
        assert "Do not mention the tier" in content
        assert "independent corroboration" in content


def test_primary_agent_blocks_conflicting_system_capabilities(repo_root: Path) -> None:
    agent = (repo_root / ".opencode/agents/mobilework.md").read_text(encoding="utf-8")

    assert "skill: deny" in agent
    assert "webfetch: deny" in agent
    assert "websearch: deny" in agent
    assert "不得调用系统级 Skill" in agent
