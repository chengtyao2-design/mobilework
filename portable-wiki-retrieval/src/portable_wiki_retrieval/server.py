from __future__ import annotations

import json
import logging
import logging.handlers
import functools
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar
from pydantic import BaseModel, ConfigDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .errors import RetrievalError
from .service import RetrievalService


server = MCPServer(
    "portable-wiki-retrieval",
    title="Portable Wiki Retrieval",
    description="注册、索引并检索本地 Markdown Wiki",
    version="0.1.0",
)
_instance: RetrievalService | None = None
F = TypeVar("F", bound=Callable[..., Any])
ProfileName = Literal["fast", "balanced", "reasoning", "research"]
RetrievalIntent = Literal[
    "exact", "semantic", "entity", "relationship",
    "temporal", "comparison", "provenance", "inventory",
]
EntityExpandMode = Literal["off", "conservative", "semantic", "semantic_required"]
RerankMode = Literal["off", "auto", "required"]
SettingsScope = Literal["global", "profile", "wiki"]


class RetrievalHints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: RetrievalIntent | None = None


def _hints_dict(value: RetrievalHints | None) -> dict[str, Any] | None:
    return value.model_dump(exclude_none=True) if value is not None else None


def _service() -> RetrievalService:
    global _instance
    if _instance is None:
        _instance = RetrievalService()
        _configure_logging(_instance.state_dir, _instance.config["runtime"]["log_level"])
    return _instance


def _configure_logging(state_dir: Path, level: str) -> None:
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("portable_wiki_retrieval")
    if logger.handlers:
        return
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "retrieval.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def _safe(function: F) -> F:
    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except RetrievalError as error:
            raise ToolError(json.dumps(error.as_dict(), ensure_ascii=False)) from error
    return wrapped  # type: ignore[return-value]


@server.tool()
@_safe
def retrieve(query: str, profile: ProfileName = "balanced", wiki_ids: list[str] | None = None,
             scope: Literal["documents"] = "documents", top_k: int = 5, include_content: bool = True,
             run_id: str | None = None, context: dict[str, Any] | None = None,
             retrieval_hints: RetrievalHints | None = None,
             information_needs: list[str] | None = None,
             entity_expand_mode: EntityExpandMode | None = None,
             rerank_mode: RerankMode | None = None) -> dict[str, Any]:
    """普通 Wiki 问答的唯一首选入口；一次调用完成规划、召回、融合和证据返回。"""
    return _service().retrieve(query, profile, wiki_ids, scope, top_k, include_content, run_id,
                               context, _hints_dict(retrieval_hints), information_needs,
                               entity_expand_mode, rerank_mode)


@server.tool()
@_safe
def prepare_query(query: str, profile: ProfileName = "balanced", context: dict[str, Any] | None = None,
                  retrieval_hints: RetrievalHints | None = None,
                  information_needs: list[str] | None = None) -> dict[str, Any]:
    """仅在用户要求解释或预览检索计划时调用；普通问答不需要先调用。"""
    return _service().prepare_query(query, profile, context, _hints_dict(retrieval_hints), information_needs)


@server.tool()
@_safe
def graph_neighbors(seed: dict[str, Any], depth: int = 1, top_k: int = 10) -> dict[str, Any]:
    """从带 wiki_id 的稳定文档 seed 开始执行有界链接图扩展。"""
    return _service().graph_neighbors(seed, depth, top_k)


@server.tool()
@_safe
def knowledge_tree(wiki_ids: list[str] | None = None) -> dict[str, Any]:
    """查看 Wiki 文档树和稳定文档标识。"""
    return _service().knowledge_tree(wiki_ids)


@server.tool()
@_safe
def list_wikis() -> dict[str, Any]:
    """列出已注册 Wiki 及索引状态。"""
    return _service().list_wikis()


@server.tool()
@_safe
def inspect_wiki(root: str, wiki_id: str = "inspection", include: list[str] | None = None,
                 exclude: list[str] | None = None) -> dict[str, Any]:
    """只读检查 Markdown 文件树是否可注册。"""
    return _service().inspect_wiki(root, wiki_id, include, exclude)


@server.tool()
@_safe
def register_wiki(wiki_id: str, root: str, name: str | None = None,
                  include: list[str] | None = None, exclude: list[str] | None = None,
                  enabled: bool = True) -> dict[str, Any]:
    """在用户明确要求时注册并索引一个 Wiki。"""
    return _service().register_wiki(wiki_id, root, name, include, exclude, enabled)


@server.tool()
@_safe
def update_wiki(wiki_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """更新 Wiki 注册信息；需要时再调用 reindex。"""
    return _service().update_wiki(wiki_id, changes)


@server.tool()
@_safe
def unregister_wiki(wiki_id: str) -> dict[str, Any]:
    """注销 Wiki 并移除派生索引，不修改原始文件。"""
    return _service().unregister_wiki(wiki_id)


@server.tool()
@_safe
def retrieval_capabilities() -> dict[str, Any]:
    """返回通道、模型和能力闸门状态。"""
    return _service().retrieval_capabilities()


@server.tool()
@_safe
def get_settings(scope: SettingsScope = "global", profile: ProfileName | None = None,
                 wiki_id: str | None = None, effective: bool = True) -> dict[str, Any]:
    """仅在用户询问配置时读取全局、profile 或 Wiki 设置；不要用于发现 retrieval intent。"""
    return _service().get_settings(scope, profile, wiki_id, effective)


@server.tool()
@_safe
def update_settings(scope: SettingsScope, changes: dict[str, Any], profile: ProfileName | None = None,
                    wiki_id: str | None = None) -> dict[str, Any]:
    """更新配置；changes 使用英文点分字段名。"""
    return _service().update_settings(scope, changes, profile, wiki_id)


@server.tool()
@_safe
def reset_settings(scope: SettingsScope, keys: list[str] | None = None, profile: ProfileName | None = None,
                   wiki_id: str | None = None) -> dict[str, Any]:
    """将指定配置恢复为内置默认值。"""
    return _service().reset_settings(scope, keys, profile, wiki_id)


def main() -> None:
    service = _service()
    service.refresh_on_start()
    server.run("stdio")


if __name__ == "__main__":
    main()
