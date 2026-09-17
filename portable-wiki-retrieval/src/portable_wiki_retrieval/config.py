from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .errors import RetrievalError, require
from .paths import default_state_dir, resolve_config_path


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "runtime": {"state_dir": "", "log_level": "INFO"},
    "embedding": {
        "mode": "remote", "backend_priority": ["remote"], "normalize": True,
        "cache_enabled": True, "cache_max_entries": 100000,
        "remote": {
            "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
            "model": "qwen/qwen3-embedding-8b", "api_key_env": "OPENROUTER_API_KEY",
            "timeout_ms": 30000, "batch_size": 32,
        },
    },
    "indexing": {
        "chunk_size_chars": 1800, "chunk_overlap_chars": 200,
        "include_heading_path": True, "heading_merge": "same-section",
        "incremental_enabled": True, "refresh_mode": "on_start",
        "stale_policy": "compatible_channels", "max_incremental_files": 1000,
        "max_incremental_bytes": 104857600, "large_directory_file_threshold": 10000,
        "large_directory_bytes_threshold": 1073741824,
        "large_directory_scan_ms_threshold": 10000,
        "disable_on_query_refresh_for_large_directory": True, "hash_workers": 4,
        "rebuild_timeout_ms": 600000, "full_validation_interval_minutes": 0,
    },
    "vector_store": {
        "backend": "lancedb", "metric": "cosine", "create_ann_index": True,
        "ann_index_type": "IVF_FLAT", "ann_min_rows": 1000,
        "target_partition_size": 256,
    },
    "fusion": {
        "rrf_k": 60.0, "bm25_weight": 1.0, "vector_weight": 1.0,
        "graph_weight": 0.35, "entity_weight": 0.35,
        "channel_candidate_pool": 30, "final_top_k": 5,
        "route_confidence": 0.5, "fallback_on_no_match": True,
        "branch_weights": {
            "exact": 1.0, "semantic": 1.0, "entity": 0.7,
            "relationship": 0.6, "temporal_current": 0.8,
            "temporal_history": 0.8, "comparison_axis": 0.8,
            "provenance": 1.0, "inventory": 1.0,
        },
    },
    "reranker": {
        "mode": "auto", "backend_priority": ["remote"], "candidate_pool": 30,
        "batch_size": 16, "timeout_ms": 10000, "latency_budget_ms": 3000,
        "remote": {
            "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
            "model": "cohere/rerank-4-pro", "api_key_env": "OPENROUTER_API_KEY",
        },
    },
    "entity_expansion": {
        "mode": "conservative", "semantic_require_capability": True,
        "semantic_require_evaluation_pass": True, "high_confidence": 0.90,
        "medium_confidence": 0.70, "low_confidence": 0.50,
        "allow_medium_candidates": True, "max_candidates_per_entity": 5,
        "max_candidates_per_wiki": 10, "semantic_timeout_ms": 5000,
        "feature_weights": {"title": 1.0, "alias": 1.0, "entity_type": 0.8,
                            "link_neighborhood": 0.6, "semantic": 0.7},
    },
    "profiles": {
        "fast": {"deadline_ms": 5000, "max_tool_calls": 1, "max_query_branches": 1,
                 "max_subqueries": 1, "allowed_channels": ["keyword", "vector"],
                 "max_graph_depth": 0, "max_evidence_chars": 6000,
                 "rewrite_mode": "original", "entity_expand_mode": "off", "rerank_mode": "off"},
        "balanced": {"deadline_ms": 12000, "max_tool_calls": 1, "max_query_branches": 2,
                     "max_subqueries": 1, "allowed_channels": ["keyword", "vector", "entity"],
                     "max_graph_depth": 0, "max_evidence_chars": 12000,
                     "rewrite_mode": "multi-route-light", "entity_expand_mode": "off",
                     "rerank_mode": "auto", "rerank_candidate_pool": 20,
                     "rerank_latency_budget_ms": 2000},
        "reasoning": {"deadline_ms": 30000, "max_tool_calls": 4, "max_query_branches": 4,
                      "max_subqueries": 3, "allowed_channels": ["keyword", "vector", "entity", "graph"],
                      "max_graph_depth": 1, "max_evidence_chars": 18000,
                      "rewrite_mode": "multi-route", "entity_expand_mode": "conservative",
                      "rerank_mode": "auto", "rerank_candidate_pool": 30,
                      "rerank_latency_budget_ms": 4000},
        "research": {"deadline_ms": 90000, "max_tool_calls": 8, "max_query_branches": 6,
                     "max_subqueries": 3, "allowed_channels": ["keyword", "vector", "entity", "graph"],
                     "max_graph_depth": 2, "max_evidence_chars": 24000,
                     "rewrite_mode": "multi-route", "entity_expand_mode": "semantic",
                     "rerank_mode": "auto", "rerank_candidate_pool": 50,
                     "rerank_latency_budget_ms": 10000},
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in base:
            raise RetrievalError("CONFIG_UNKNOWN_KEY", f"未知配置项：{path}")
        if isinstance(base[key], dict):
            require(isinstance(value, dict), "CONFIG_INVALID", f"{path} 必须是表")
            result[key] = _merge(base[key], value, path)
        else:
            if type(value) is not type(base[key]) and not (
                isinstance(base[key], float) and isinstance(value, int) and not isinstance(value, bool)
            ):
                raise RetrievalError("CONFIG_INVALID", f"{path} 类型不正确")
            result[key] = value
    return result


def validate(config: dict[str, Any]) -> None:
    require(config["version"] == 1, "CONFIG_INVALID", "仅支持配置版本 1")
    for section in (config["embedding"]["remote"], config["reranker"]["remote"]):
        env_name = section["api_key_env"]
        require(env_name == "OPENROUTER_API_KEY", "CONFIG_INVALID",
                "阶段一凭据变量名必须是 OPENROUTER_API_KEY，不能在配置中填写真实密钥")
        require(bool(re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name)), "CONFIG_INVALID",
                "api_key_env 必须是环境变量名")
    indexing = config["indexing"]
    require(indexing["chunk_size_chars"] > 0, "CONFIG_OUT_OF_RANGE", "chunk_size_chars 必须大于 0")
    require(0 <= indexing["chunk_overlap_chars"] < indexing["chunk_size_chars"],
            "CONFIG_OUT_OF_RANGE", "chunk_overlap_chars 必须小于 chunk_size_chars")
    vector_store = config["vector_store"]
    require(vector_store["backend"] == "lancedb", "CONFIG_INVALID", "阶段一向量后端必须是 lancedb")
    require(vector_store["metric"] in {"cosine", "l2", "dot"}, "CONFIG_INVALID", "LanceDB 距离类型无效")
    require(vector_store["ann_index_type"] in {
        "IVF_FLAT", "IVF_SQ", "IVF_PQ", "IVF_RQ", "IVF_HNSW_SQ", "IVF_HNSW_PQ", "IVF_HNSW_FLAT"
    }, "CONFIG_INVALID", "LanceDB ANN 索引类型无效")
    require(vector_store["ann_min_rows"] >= 1 and vector_store["target_partition_size"] >= 1,
            "CONFIG_OUT_OF_RANGE", "LanceDB ANN 阈值和分区大小必须为正数")
    fusion = config["fusion"]
    require(1 <= float(fusion["rrf_k"]) <= 10000, "CONFIG_OUT_OF_RANGE", "rrf_k 必须在 1 到 10000")
    weights = [fusion[name] for name in ("bm25_weight", "vector_weight", "graph_weight", "entity_weight")]
    require(all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in weights),
            "CONFIG_OUT_OF_RANGE", "融合权重必须是有限非负数")
    require(any(v > 0 for v in weights), "CONFIG_OUT_OF_RANGE", "至少一个融合权重必须大于 0")
    entity = config["entity_expansion"]
    require(entity["high_confidence"] > entity["medium_confidence"] > entity["low_confidence"],
            "CONFIG_OUT_OF_RANGE", "实体阈值必须满足 high > medium > low")
    for name, profile in config["profiles"].items():
        require(profile["max_query_branches"] >= 1 and profile["max_subqueries"] >= 1,
                "CONFIG_OUT_OF_RANGE", f"profiles.{name} 的预算必须为正数")
        require(set(profile["allowed_channels"]) <= {"keyword", "vector", "entity", "graph"},
                "CONFIG_INVALID", f"profiles.{name}.allowed_channels 含未知通道")


def load(path: str | Path | None = None) -> tuple[dict[str, Any], Path]:
    target = resolve_config_path(path)
    override: dict[str, Any] = {}
    if target.is_file():
        try:
            override = tomllib.loads(target.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise RetrievalError("CONFIG_INVALID", f"无法读取配置：{error}") from error
    config = _merge(DEFAULT_CONFIG, override)
    validate(config)
    return config, target


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = _merge(base, override)
    validate(result)
    return result


def state_dir(config: dict[str, Any]) -> Path:
    raw = os.environ.get("PORTABLE_WIKI_STATE_DIR") or config["runtime"]["state_dir"]
    return Path(raw).expanduser().resolve() if raw else default_state_dir()


def fingerprint(config: dict[str, Any], keys: tuple[str, ...] = ("embedding", "indexing", "vector_store")) -> str:
    selected = {key: config[key] for key in keys}
    return hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def get_dotted(data: dict[str, Any], key: str) -> Any:
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise RetrievalError("CONFIG_UNKNOWN_KEY", f"未知配置项：{key}")
        current = current[part]
    return current


def set_dotted(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    current = data
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            raise RetrievalError("CONFIG_UNKNOWN_KEY", f"未知配置项：{key}")
        current = current[part]
    if parts[-1] not in current:
        raise RetrievalError("CONFIG_UNKNOWN_KEY", f"未知配置项：{key}")
    current[parts[-1]] = value


def parse_cli_value(raw: str) -> Any:
    try:
        return tomllib.loads(f"value = {raw}")["value"]
    except tomllib.TOMLDecodeError:
        return raw


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"不支持的 TOML 值：{type(value).__name__}")


def dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")

    def emit(prefix: str, table: dict[str, Any]) -> None:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"[{prefix}]")
        children: list[tuple[str, dict[str, Any]]] = []
        for key, value in table.items():
            if isinstance(value, dict):
                children.append((key, value))
            else:
                lines.append(f"{key} = {_toml_value(value)}")
        for key, child in children:
            emit(f"{prefix}.{key}", child)

    for name, table in tables.items():
        emit(name, table)
    return "\n".join(lines).rstrip() + "\n"


def write(config: dict[str, Any], path: Path) -> None:
    validate(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dump_toml(config), encoding="utf-8")
    os.replace(temporary, path)
