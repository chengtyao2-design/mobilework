from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config as config_module
from .errors import RetrievalError, require
from .indexer import IndexStore, build_index, freshness
from .markdown import normalize_text, scan_documents, search_tokens
from .openrouter import OpenRouterClient
from .paths import load_dotenv
from .planner import build_query_plan
from .registry import DEFAULT_EXCLUDES, WikiRegistry


logger = logging.getLogger("portable_wiki_retrieval")


class RunManager:
    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def begin(self, run_id: str | None, profile_name: str, profile: dict[str, Any], query: str) -> tuple[str, dict[str, Any]]:
        query_hash = hashlib.sha256(normalize_text(query).encode("utf-8")).hexdigest()
        with self._lock:
            if run_id is None:
                run_id = f"run_{uuid.uuid4().hex[:16]}"
                self._runs[run_id] = {
                    "profile": profile_name, "calls": 0, "elapsed_ms": 0.0,
                    "query_hashes": set(), "created_monotonic": time.monotonic(),
                }
            state = self._runs.get(run_id)
            require(state is not None, "INVALID_ARGUMENT", "run_id 不存在或已经过期")
            require(state["profile"] == profile_name, "INVALID_ARGUMENT", "同一 run_id 不能切换 profile")
            require(state["calls"] < int(profile["max_tool_calls"]), "BUDGET_EXHAUSTED", "本轮 MCP 调用预算已耗尽")
            require(query_hash not in state["query_hashes"], "DUPLICATE_QUERY", "本轮已经执行等价 Query")
            require(state["elapsed_ms"] < float(profile["deadline_ms"]), "BUDGET_EXHAUSTED", "本轮时间预算已耗尽")
            state["calls"] += 1
            state["query_hashes"].add(query_hash)
            return run_id, dict(state)

    def finish(self, run_id: str, elapsed_ms: float) -> dict[str, Any]:
        with self._lock:
            state = self._runs[run_id]
            state["elapsed_ms"] += elapsed_ms
            return {key: value for key, value in state.items() if key not in {"query_hashes", "created_monotonic"}}


class RetrievalService:
    def __init__(self, config_path: str | Path | None = None, *, client: OpenRouterClient | None = None):
        self.config_path_arg = config_path
        self.config, self.config_path = config_module.load(config_path)
        load_dotenv(self.config_path, override=True)
        self.state_dir = config_module.state_dir(self.config)
        self.registry = WikiRegistry(self.state_dir)
        self.client = client or OpenRouterClient(self.config)
        self.runs = RunManager()

    def reload(self) -> None:
        self.config, self.config_path = config_module.load(self.config_path_arg)
        load_dotenv(self.config_path, override=True)
        self.state_dir = config_module.state_dir(self.config)
        self.registry = WikiRegistry(self.state_dir)
        if type(self.client) is OpenRouterClient:
            self.client = OpenRouterClient(self.config)

    def effective_config(self, entry: dict[str, Any] | None = None) -> dict[str, Any]:
        return config_module.merge(self.config, entry.get("settings", {})) if entry and entry.get("settings") else copy.deepcopy(self.config)

    @staticmethod
    def _safe_settings(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: RetrievalService._safe_settings(item) for key, item in value.items()}
        if isinstance(value, list):
            return [RetrievalService._safe_settings(item) for item in value]
        if isinstance(value, str) and (value.startswith("sk-") or value.startswith("sk_or_") or len(value) > 40 and "key" in value.casefold()):
            return "[redacted]"
        return value

    def inspect_wiki(self, root: str, wiki_id: str = "inspection", include: list[str] | None = None,
                     exclude: list[str] | None = None) -> dict[str, Any]:
        entry = {"wiki_id": wiki_id, "root": str(Path(root).expanduser().resolve()),
                 "include": include or ["**/*.md"], "exclude": exclude or list(DEFAULT_EXCLUDES)}
        documents, report = scan_documents(entry)
        titles: dict[str, list[str]] = {}
        for document in documents:
            titles.setdefault(normalize_text(document.title), []).append(document.relative_path)
        report["title_conflicts"] = {title: paths for title, paths in titles.items() if len(paths) > 1}
        report["compatible"] = bool(documents)
        report["recommendation"] = "ready" if documents else "no_documents"
        return report

    def register_wiki(self, wiki_id: str, root: str, name: str | None = None,
                      include: list[str] | None = None, exclude: list[str] | None = None,
                      enabled: bool = True) -> dict[str, Any]:
        inspection = self.inspect_wiki(root, wiki_id, include, exclude)
        require(inspection["files"] > 0, "NO_DOCUMENTS", "目录中没有匹配的 Markdown 文件")
        entry = self.registry.register(wiki_id, root, name=name, include=include, exclude=exclude, enabled=enabled)
        try:
            indexed = build_index(entry, self.state_dir, self.effective_config(entry), self.client)
        except Exception:
            self.registry.unregister(wiki_id, remove_index=True)
            raise
        return {"status": indexed["status"], "wiki": entry, "inspection": inspection, "index": indexed}

    def update_wiki(self, wiki_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        entry = self.registry.update(wiki_id, **changes) if changes else self.registry.get(wiki_id)
        indexed = build_index(entry, self.state_dir, self.effective_config(entry), self.client)
        return {"status": "updated", "wiki": entry, "requires_reindex": False, "index": indexed}

    def unregister_wiki(self, wiki_id: str) -> dict[str, Any]:
        return self.registry.unregister(wiki_id)

    def list_wikis(self) -> dict[str, Any]:
        items = []
        for entry in self.registry.list():
            items.append({**entry, "index": freshness(entry, self.state_dir, self.effective_config(entry))})
        return {"wikis": items}

    def reindex(self, wiki_id: str) -> dict[str, Any]:
        entry = self.registry.get(wiki_id, require_enabled=True)
        return build_index(entry, self.state_dir, self.effective_config(entry), self.client)

    def refresh_on_start(self) -> list[dict[str, Any]]:
        results = []
        if self.config["indexing"]["refresh_mode"] != "on_start":
            return results
        for entry in self.registry.list(enabled_only=True):
            state = freshness(entry, self.state_dir, self.effective_config(entry))
            if state.get("stale"):
                try:
                    results.append(self.reindex(entry["wiki_id"]))
                except RetrievalError as error:
                    results.append({"wiki_id": entry["wiki_id"], "status": "failed", "error": error.as_dict()})
        return results

    def prepare_query(self, query: str, profile: str = "balanced", context: dict[str, Any] | None = None,
                      retrieval_hints: dict[str, Any] | None = None,
                      information_needs: list[str] | None = None) -> dict[str, Any]:
        self.reload()
        require(profile in self.config["profiles"], "INVALID_PROFILE", f"未知检索档位：{profile}")
        return build_query_plan(query, profile, self.config["profiles"][profile], context=context,
                                retrieval_hints=retrieval_hints, information_needs=information_needs)

    def _select_wikis(self, query: str, wiki_ids: list[str] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        entries = {item["wiki_id"]: item for item in self.registry.list(enabled_only=True)}
        require(bool(entries), "WIKI_NOT_FOUND", "没有已注册且启用的 Wiki")
        if wiki_ids:
            requested = list(dict.fromkeys(wiki_ids))
            missing = [wiki_id for wiki_id in requested if wiki_id not in entries]
            require(not missing, "WIKI_NOT_FOUND", "指定 Wiki 未注册或已禁用", wiki_ids=missing)
            return [entries[wiki_id] for wiki_id in requested], {
                "mode": "explicit", "candidate_wiki_ids": requested, "selected_wiki_ids": requested,
                "hard_boundary": True,
            }
        tokens = set(search_tokens(query))
        scored = []
        for entry in entries.values():
            catalog = " ".join([entry["wiki_id"], entry.get("name", "")])
            try:
                with IndexStore(self.state_dir, entry["wiki_id"]) as store:
                    catalog += " " + " ".join(doc["title"] for doc in store.tree()["documents"])
            except RetrievalError:
                pass
            catalog_tokens = set(search_tokens(catalog))
            score = len(tokens & catalog_tokens) / max(1, len(tokens))
            scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["wiki_id"]))
        threshold = float(self.config["fusion"]["route_confidence"])
        selected = [entry for score, entry in scored if score >= threshold]
        fallback = not selected
        if fallback:
            selected = [entry for _, entry in scored]
        return selected, {
            "mode": "automatic", "candidate_wiki_ids": sorted(entries),
            "selected_wiki_ids": [entry["wiki_id"] for entry in selected], "hard_boundary": False,
            "scores": [{"wiki_id": entry["wiki_id"], "score": score} for score, entry in scored],
            "fallback": fallback,
        }

    @staticmethod
    def _channel_weight(config: dict[str, Any], channel: str) -> float:
        return float(config["fusion"][{"keyword": "bm25_weight", "vector": "vector_weight",
                                       "graph": "graph_weight", "entity": "entity_weight"}[channel]])

    def _semantic_gate(self, config: dict[str, Any]) -> tuple[bool, list[str]]:
        setting = config["entity_expansion"]
        reasons = []
        if setting["semantic_require_capability"] and not (
            self.client.embedding_available() and self.client.reranker_available()
        ):
            reasons.append("capability_unavailable")
        marker = self.state_dir / "evaluations" / "entity-semantic-pass.json"
        if setting["semantic_require_evaluation_pass"] and not marker.is_file():
            reasons.append("evaluation_not_passed")
        return not reasons, reasons

    def retrieve(
        self, query: str, profile: str = "balanced", wiki_ids: list[str] | None = None,
        scope: str = "documents", top_k: int | None = None, include_content: bool = True,
        run_id: str | None = None, context: dict[str, Any] | None = None,
        retrieval_hints: dict[str, Any] | None = None,
        information_needs: list[str] | None = None,
        entity_expand_mode: str | None = None, rerank_mode: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        self.reload()
        require(scope == "documents", "INVALID_ARGUMENT", "阶段一 scope 固定为 documents")
        require(profile in self.config["profiles"], "INVALID_PROFILE", f"未知检索档位：{profile}")
        profile_config = self.config["profiles"][profile]
        run_id, run_before = self.runs.begin(run_id, profile, profile_config, query)
        plan = build_query_plan(query, profile, profile_config, context=context,
                                retrieval_hints=retrieval_hints, information_needs=information_needs)
        entries, routing = self._select_wikis(plan["resolved_query"], wiki_ids)
        pool = int(self.config["fusion"]["channel_candidate_pool"])
        limit = int(top_k or self.config["fusion"]["final_top_k"])
        require(1 <= limit <= 50, "INVALID_ARGUMENT", "top_k 必须在 1 到 50")
        degradations: list[dict[str, Any]] = []
        errors: dict[str, Any] = {}
        index_states: dict[str, Any] = {}
        stores: dict[str, IndexStore] = {}
        for entry in entries:
            wiki_config = self.effective_config(entry)
            state = freshness(entry, self.state_dir, wiki_config)
            index_states[entry["wiki_id"]] = state
            if not state.get("exists") or (state.get("stale") and wiki_config["indexing"]["refresh_mode"] == "on_query"):
                try:
                    build_index(entry, self.state_dir, wiki_config, self.client)
                    state = freshness(entry, self.state_dir, wiki_config)
                    index_states[entry["wiki_id"]] = state
                except RetrievalError as error:
                    errors[entry["wiki_id"]] = error.as_dict()
                    continue
            if state.get("stale"):
                degradations.append({"code": "INDEX_STALE", "wiki_id": entry["wiki_id"], "details": state})
            try:
                stores[entry["wiki_id"]] = IndexStore(self.state_dir, entry["wiki_id"])
            except RetrievalError as error:
                errors[entry["wiki_id"]] = error.as_dict()

        require(bool(stores), "INDEX_STALE", "没有可用 Wiki 索引", errors=errors)
        branch_vectors: dict[str, list[float]] = {}
        vector_branches = [branch for branch in plan["branches"] if "vector" in branch["target_channels"]]
        embedding_state: dict[str, Any] = {
            "requested": bool(vector_branches), "status": "not_requested", "model": self.config["embedding"]["remote"]["model"]
        }
        if vector_branches:
            if self.client.embedding_available():
                try:
                    vectors, latency = self.client.embed([branch["query"] for branch in vector_branches], query=True)
                    branch_vectors = {branch["branch_id"]: vector for branch, vector in zip(vector_branches, vectors, strict=True)}
                    embedding_state.update(status="available", latency_ms=latency)
                except RetrievalError as error:
                    embedding_state.update(status="degraded", error=error.as_dict())
                    degradations.append(error.as_dict())
            else:
                embedding_state.update(status="disabled", reason="OPENROUTER_API_KEY 未设置")
                degradations.append({"code": "VECTOR_UNAVAILABLE", "message": "向量通道已降级为其他允许通道"})

        runs: list[tuple[str, str, str, list[dict[str, Any]]]] = []
        try:
            for branch in plan["branches"]:
                for wiki_id, store in stores.items():
                    for channel in branch["target_channels"]:
                        hits: list[dict[str, Any]] = []
                        if channel == "keyword":
                            hits = store.keyword_search(branch["query"], pool)
                        elif channel == "vector" and branch["branch_id"] in branch_vectors:
                            hits = store.vector_search(branch_vectors[branch["branch_id"]], pool)
                        elif channel == "entity":
                            hits = store.entity_search(branch["query"], pool)
                        elif channel == "graph":
                            continue
                        runs.append((branch["branch_id"], branch["purpose"], channel, hits))

            candidates: dict[str, dict[str, Any]] = {}

            def fuse_run(branch_id: str, purpose: str, channel: str, hits: list[dict[str, Any]]) -> None:
                branch_weight = float(self.config["fusion"]["branch_weights"].get(purpose, 1.0))
                channel_weight = self._channel_weight(self.config, channel)
                rrf_k = float(self.config["fusion"]["rrf_k"])
                for rank, hit in enumerate(hits, 1):
                    key = f"{hit['wiki_id']}::{hit['chunk_id']}"
                    item = candidates.setdefault(key, {
                        "hit": hit, "rrf_score": 0.0, "channels": set(), "branches": set(), "ranks": {},
                    })
                    item["rrf_score"] += branch_weight * channel_weight / (rrf_k + rank)
                    item["channels"].add(channel)
                    item["branches"].add(branch_id)
                    item["ranks"][f"{branch_id}:{channel}"] = rank

            for run in runs:
                fuse_run(*run)

            # Relationship branches use a stable document hit as their internal graph seed.
            for branch in plan["branches"]:
                if "graph" not in branch["target_channels"]:
                    continue
                for wiki_id, store in stores.items():
                    seed_candidates = [value for value in candidates.values()
                                       if value["hit"]["wiki_id"] == wiki_id and branch["branch_id"] in value["branches"]]
                    seed_candidates.sort(key=lambda item: -item["rrf_score"])
                    if not seed_candidates:
                        branch.setdefault("skipped", []).append({"channel": "graph", "reason": "no_stable_seed"})
                        continue
                    seed = seed_candidates[0]["hit"]["document_id"]
                    graph_hits = store.graph_neighbors(seed, int(profile_config["max_graph_depth"]), pool)
                    fuse_run(branch["branch_id"], branch["purpose"], "graph", graph_hits)
                    branch["graph_seed"] = {"wiki_id": wiki_id, "document_id": seed}

            requested_entity_mode = entity_expand_mode or profile_config["entity_expand_mode"]
            require(requested_entity_mode in {"off", "conservative", "semantic", "semantic_required"},
                    "INVALID_ARGUMENT", "未知 entity_expand_mode")
            effective_entity_mode = requested_entity_mode
            entity_status: dict[str, Any] = {"requested": requested_entity_mode, "status": "off", "matches": []}
            if requested_entity_mode in {"semantic", "semantic_required"}:
                allowed, reasons = self._semantic_gate(self.config)
                if not allowed and requested_entity_mode == "semantic_required":
                    raise RetrievalError("ENTITY_EXPANSION_UNAVAILABLE", "semantic 实体扩展能力闸门未通过", {"reasons": reasons})
                if not allowed:
                    effective_entity_mode = "conservative"
                    degradations.append({"code": "ENTITY_EXPANSION_UNAVAILABLE", "message": "semantic 已降级为 conservative", "reasons": reasons})
            if effective_entity_mode != "off" and len(stores) > 1 and candidates:
                top_sources = sorted(candidates.values(), key=lambda item: -item["rrf_score"])[:5]
                expansion_runs: list[tuple[str, str, str, list[dict[str, Any]]]] = []
                high = float(self.config["entity_expansion"]["high_confidence"])
                for source in top_sources:
                    hit = source["hit"]
                    for name in [hit["title"], *hit.get("aliases", [])]:
                        for wiki_id, store in stores.items():
                            if wiki_id == hit["wiki_id"]:
                                continue
                            matches = store.entity_search(name, int(self.config["entity_expansion"]["max_candidates_per_wiki"]))
                            compatible = [match for match in matches if not (
                                hit.get("entity_type") and match.get("entity_type") and hit["entity_type"] != match["entity_type"]
                            )]
                            if compatible:
                                confidence = 0.95
                                if confidence >= high:
                                    expansion_runs.append((next(iter(source["branches"])), "entity", "entity", compatible))
                                    entity_status["matches"].append({
                                        "canonical_name": name, "entity_type": hit.get("entity_type"),
                                        "confidence": confidence, "signals": ["exact_title_or_alias"],
                                        "source_wiki_id": hit["wiki_id"], "target_wiki_id": wiki_id,
                                    })
                for run in expansion_runs:
                    fuse_run(*run)
                entity_status["status"] = "expanded" if expansion_runs else "no_match"
            elif effective_entity_mode != "off":
                entity_status["status"] = "no_match"
            entity_status["effective"] = effective_entity_mode

            ranking = sorted(candidates.values(), key=lambda item: (-item["rrf_score"], item["hit"]["wiki_id"], item["hit"]["relative_path"]))
            rerank_requested = rerank_mode or profile_config["rerank_mode"]
            require(rerank_requested in {"off", "auto", "required"}, "INVALID_ARGUMENT", "未知 rerank_mode")
            rerank_state: dict[str, Any] = {"requested": rerank_requested, "status": "off",
                                                   "model": self.config["reranker"]["remote"]["model"]}
            if rerank_requested != "off" and ranking:
                candidate_pool = min(len(ranking), int(profile_config.get("rerank_candidate_pool", self.config["reranker"]["candidate_pool"])))
                if not self.client.reranker_available():
                    if rerank_requested == "required":
                        raise RetrievalError("RERANKER_UNAVAILABLE", "未设置 OpenRouter reranker 凭据")
                    rerank_state.update(status="degraded", reason="OPENROUTER_API_KEY 未设置")
                    degradations.append({"code": "RERANKER_UNAVAILABLE", "message": "reranker 已降级为 RRF"})
                else:
                    try:
                        subset = ranking[:candidate_pool]
                        scored, latency = self.client.rerank(plan["resolved_query"], [item["hit"]["text"] for item in subset], candidate_pool)
                        score_map = {row["index"]: row["score"] for row in scored}
                        for index, item in enumerate(subset):
                            item["rerank_score"] = score_map.get(index, float("-inf"))
                        subset.sort(key=lambda item: (-item["rerank_score"], -item["rrf_score"]))
                        ranking = subset + ranking[candidate_pool:]
                        rerank_state.update(status="available", backend="openrouter", latency_ms=latency)
                    except RetrievalError as error:
                        if rerank_requested == "required":
                            raise
                        rerank_state.update(status="degraded", error=error.as_dict())
                        degradations.append(error.as_dict())

            evidence_budget = int(profile_config["max_evidence_chars"])
            used = 0
            results = []
            for item in ranking[:limit]:
                hit = item["hit"]
                text = hit["text"] if include_content else None
                if text is not None:
                    remaining = max(0, evidence_budget - used)
                    text = text[:remaining]
                    used += len(text)
                evidence = {"chunk_id": hit["chunk_id"], "heading": hit["heading_path"],
                            "heading_path": hit["heading_path"]}
                if text is not None and text:
                    evidence["text"] = text
                result = {
                    "wiki_id": hit["wiki_id"], "document_id": hit["document_id"],
                    "path": hit["relative_path"], "relative_path": hit["relative_path"], "title": hit["title"],
                    "score": item["rrf_score"], "rerank_score": item.get("rerank_score"),
                    "channels": sorted(item["channels"]), "branches": sorted(item["branches"]),
                    "evidence": [evidence],
                }
                results.append(result)

            elapsed_ms = (time.perf_counter() - started) * 1000
            budget = self.runs.finish(run_id, elapsed_ms)
            stop_reason = "results_ready" if results else "no_match"
            if elapsed_ms > float(profile_config["deadline_ms"]):
                degradations.append({"code": "BUDGET_EXHAUSTED", "message": "本次调用超过档位 deadline"})
                stop_reason = "budget_exhausted"
            return {
                "run_id": run_id, "profile": profile, "query_plan": plan,
                "routing": routing, "queried_wiki_ids": sorted(stores), "results": results,
                "index": index_states, "embedding": embedding_state, "reranker": rerank_state,
                "entity_expansion": entity_status,
                "budget": {**budget, "deadline_ms": profile_config["deadline_ms"],
                           "max_tool_calls": profile_config["max_tool_calls"],
                           "max_query_branches": profile_config["max_query_branches"],
                           "max_subqueries": profile_config["max_subqueries"],
                           "evidence_chars": used},
                "degradation": degradations, "partial_failure": bool(errors), "errors": errors,
                "stop_reason": stop_reason,
            }
        finally:
            for store in stores.values():
                store.close()

    def graph_neighbors(self, seed: dict[str, Any], depth: int = 1, top_k: int = 10) -> dict[str, Any]:
        wiki_id = str(seed.get("wiki_id", ""))
        require(bool(wiki_id), "INVALID_ARGUMENT", "seed 必须包含 wiki_id")
        entry = self.registry.get(wiki_id, require_enabled=True)
        profile_max = max(int(item["max_graph_depth"]) for item in self.config["profiles"].values())
        require(0 <= depth <= profile_max, "INVALID_ARGUMENT", f"depth 必须在 0 到 {profile_max}")
        with IndexStore(self.state_dir, wiki_id) as store:
            document_id = store.resolve_seed(seed.get("document_id"), seed.get("relative_path"), seed.get("title"))
            results = store.graph_neighbors(document_id, depth, top_k)
        return {"seed": {"wiki_id": wiki_id, "document_id": document_id}, "depth": depth,
                "results": [{"wiki_id": wiki_id, **item} for item in results]}

    def knowledge_tree(self, wiki_ids: list[str] | None = None) -> dict[str, Any]:
        entries = self.registry.list(enabled_only=True)
        if wiki_ids:
            allowed = set(wiki_ids)
            entries = [entry for entry in entries if entry["wiki_id"] in allowed]
            require(len(entries) == len(allowed), "WIKI_NOT_FOUND", "存在未注册或禁用的 Wiki")
        trees = []
        for entry in entries:
            with IndexStore(self.state_dir, entry["wiki_id"]) as store:
                trees.append(store.tree())
        return {"wikis": trees}

    def retrieval_capabilities(self) -> dict[str, Any]:
        gate, reasons = self._semantic_gate(self.config)
        return {
            "server_version": "0.1.0", "transport": "stdio",
            "profiles": sorted(self.config["profiles"]),
            "channels": ["keyword", "vector", "entity", "graph"],
            "embedding": {"available": self.client.embedding_available(),
                          "model": self.config["embedding"]["remote"]["model"]},
            "reranker": {"available": self.client.reranker_available(),
                         "model": self.config["reranker"]["remote"]["model"]},
            "entity_semantic": {"available": gate, "reasons": reasons},
        }

    def get_settings(self, scope: str = "global", profile: str | None = None,
                     wiki_id: str | None = None, effective: bool = True) -> dict[str, Any]:
        self.reload()
        if scope == "global":
            return {"scope": scope, "settings": self._safe_settings(self.config), "source": str(self.config_path)}
        if scope == "profile":
            require(profile in self.config["profiles"], "INVALID_PROFILE", "未知 profile")
            return {"scope": scope, "profile": profile, "settings": self._safe_settings(self.config["profiles"][profile]),
                    "source": f"profile:{profile}"}
        if scope == "wiki":
            require(bool(wiki_id), "INVALID_ARGUMENT", "wiki scope 需要 wiki_id")
            entry = self.registry.get(wiki_id or "")
            return {"scope": scope, "wiki_id": wiki_id,
                    "settings": self._safe_settings(self.effective_config(entry) if effective else entry.get("settings", {})),
                    "source": f"wiki:{wiki_id}" if entry.get("settings") else "global"}
        raise RetrievalError("CONFIG_SCOPE_FORBIDDEN", f"未知配置 scope：{scope}")

    @staticmethod
    def _nested_changes(changes: dict[str, Any]) -> dict[str, Any]:
        root: dict[str, Any] = {}
        for dotted, value in changes.items():
            current = root
            parts = dotted.split(".")
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = value
        return root

    def update_settings(self, scope: str, changes: dict[str, Any], profile: str | None = None,
                        wiki_id: str | None = None) -> dict[str, Any]:
        require(bool(changes), "INVALID_ARGUMENT", "changes 不能为空")
        normalized = {str(key): value for key, value in changes.items()}
        if scope == "global":
            updated = copy.deepcopy(self.config)
            for key, value in normalized.items():
                config_module.set_dotted(updated, key, value)
            config_module.write(updated, self.config_path)
        elif scope == "profile":
            require(profile in self.config["profiles"], "INVALID_PROFILE", "未知 profile")
            updated = copy.deepcopy(self.config)
            for key, value in normalized.items():
                dotted = key if key.startswith("profiles.") else f"profiles.{profile}.{key}"
                config_module.set_dotted(updated, dotted, value)
            config_module.write(updated, self.config_path)
        elif scope == "wiki":
            require(bool(wiki_id), "INVALID_ARGUMENT", "wiki scope 需要 wiki_id")
            entry = self.registry.get(wiki_id or "")
            overrides = copy.deepcopy(entry.get("settings", {}))
            nested = self._nested_changes(normalized)
            effective = config_module.merge(self.effective_config(entry), nested)
            # Store only the explicit nested values while validation uses the effective result.
            del effective
            def deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
                for key, value in source.items():
                    if isinstance(value, dict):
                        deep_update(target.setdefault(key, {}), value)
                    else:
                        target[key] = value
            deep_update(overrides, nested)
            self.registry.update(wiki_id or "", settings=overrides)
        else:
            raise RetrievalError("CONFIG_SCOPE_FORBIDDEN", f"未知配置 scope：{scope}")
        self.reload()
        reindex = any(key.startswith(("embedding.", "indexing.")) for key in normalized)
        return {"status": "updated", "changed": self._safe_settings(normalized),
                "applies": "after_reindex" if reindex else "next_request",
                "requires_reindex": reindex, "requires_restart": False,
                "affected_wiki_ids": [wiki_id] if wiki_id else [entry["wiki_id"] for entry in self.registry.list()] if reindex else []}

    def reset_settings(self, scope: str, keys: list[str] | None = None, profile: str | None = None,
                       wiki_id: str | None = None) -> dict[str, Any]:
        if scope == "wiki":
            require(bool(wiki_id), "INVALID_ARGUMENT", "wiki scope 需要 wiki_id")
            entry = self.registry.get(wiki_id or "")
            if keys:
                settings = copy.deepcopy(entry.get("settings", {}))
                for key in keys:
                    parts = key.split(".")
                    current = settings
                    for part in parts[:-1]:
                        current = current.get(part, {})
                    current.pop(parts[-1], None)
                self.registry.update(wiki_id or "", settings=settings)
            else:
                self.registry.update(wiki_id or "", settings={})
            return {"status": "reset", "scope": scope, "wiki_id": wiki_id}
        updated = copy.deepcopy(self.config)
        targets = keys or ([] if scope == "profile" else list(config_module.DEFAULT_CONFIG))
        if scope == "profile":
            require(profile in self.config["profiles"], "INVALID_PROFILE", "未知 profile")
            if not keys:
                updated["profiles"][profile or ""] = copy.deepcopy(config_module.DEFAULT_CONFIG["profiles"][profile or ""])
            else:
                for key in keys:
                    dotted = key if key.startswith("profiles.") else f"profiles.{profile}.{key}"
                    config_module.set_dotted(updated, dotted, config_module.get_dotted(config_module.DEFAULT_CONFIG, dotted))
        elif scope == "global":
            for key in targets:
                if "." in key:
                    config_module.set_dotted(
                        updated, key, copy.deepcopy(config_module.get_dotted(config_module.DEFAULT_CONFIG, key))
                    )
                else:
                    updated[key] = copy.deepcopy(config_module.DEFAULT_CONFIG[key])
        else:
            raise RetrievalError("CONFIG_SCOPE_FORBIDDEN", f"未知配置 scope：{scope}")
        config_module.write(updated, self.config_path)
        self.reload()
        return {"status": "reset", "scope": scope, "profile": profile, "keys": keys or []}
