from __future__ import annotations

import re
import unicodedata
import hashlib
import json
from typing import Any

from .errors import RetrievalError, require


INTENTS = {"exact", "semantic", "entity", "relationship", "temporal", "comparison", "provenance", "inventory"}
NEGATIONS = ("不", "不是", "不要", "未", "没有", "禁止", "不得", "无")
CURRENT_TERMS = ("当前", "现在", "现行", "最新", "目前", "如今")
LATER_TERMS = ("后来", "后来的", "后续", "之后", "此后", "新规", "新法", "新制度", "新版本")
HISTORY_TERMS = ("以前", "过去", "历史", "原来", "曾经", "旧版", "旧规", "旧法", "当时", "此前", "原规定")
TEMPORAL_PAIR_TERMS = ("新旧", "前后", "历次", "沿革")
RELATION_TERMS = ("关系", "关联", "依赖", "链接", "引用", "上下游", "相关页面")
PROVENANCE_TERMS = ("来源", "出处", "依据", "引用自", "原文")
INVENTORY_TERMS = ("有哪些页面", "全部页面", "目录", "清单", "列出所有")
COMPARE_TERMS = ("比较", "对比", "区别", "差异", "不同", "相比", "和", "与")
YEAR_PATTERN = r"(?:19|20)\d{2}\s*年?"


def normalize_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", query).strip().split())


def _resolve_context(query: str, context: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    resolved = query
    entities = context.get("resolved_entities", []) if isinstance(context, dict) else []
    valid: list[dict[str, Any]] = []
    for item in entities if isinstance(entities, list) else []:
        if not isinstance(item, dict):
            continue
        mention = str(item.get("mention", "")).strip()
        canonical = str(item.get("canonical_name", "")).strip()
        if mention and canonical:
            resolved = resolved.replace(mention, canonical)
            valid.append({
                "mention": mention, "canonical_name": canonical,
                "entity_type": item.get("entity_type"), "source": item.get("source", "context"),
            })
    return resolved, valid


def _protected(query: str, entities: list[dict[str, Any]]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for match in re.finditer(r"[\"“”']([^\"“”']+)[\"“”']", query):
        values.append({"type": "quoted_phrase", "value": match.group(1)})
    for match in re.finditer(r"\b\d+(?:\.\d+)?(?:年|月|日|天|%|％)?\b", query):
        values.append({"type": "number_or_date", "value": match.group(0)})
    for term in NEGATIONS:
        if term in query:
            values.append({"type": "negation", "value": term})
    for entity in entities:
        values.append({"type": "entity", "value": str(entity["canonical_name"])})
    return list({(item["type"], item["value"]): item for item in values}.values())


def _has_temporal_pair(query: str) -> bool:
    has_year = bool(re.search(YEAR_PATTERN, query))
    has_history = has_year or any(term in query for term in HISTORY_TERMS)
    has_later = any(term in query for term in CURRENT_TERMS + LATER_TERMS)
    has_explicit_pair = any(term in query for term in TEMPORAL_PAIR_TERMS)
    return has_explicit_pair or (has_history and has_later)


def detect_intent(query: str, hint: str | None = None) -> tuple[str, list[str]]:
    if hint:
        require(hint in INTENTS, "INVALID_ARGUMENT", f"未知 retrieval intent：{hint}")
        # Skill hints are high-level guidance, not a complete query plan. A generic
        # comparison hint must not hide an explicit old-versus-new time contrast.
        if hint == "comparison" and _has_temporal_pair(query):
            return "temporal", ["temporal_pair_terms", "skill_hint_refined"]
        return hint, ["skill_hint"]
    if any(term in query for term in INVENTORY_TERMS):
        return "inventory", ["inventory_terms"]
    # An explicit old/new pair is more specific than a request for sources. For
    # example, "把新旧依据分开" needs two temporal branches, not provenance-only.
    if _has_temporal_pair(query):
        return "temporal", ["temporal_pair_terms"]
    if any(term in query for term in PROVENANCE_TERMS):
        return "provenance", ["provenance_terms"]
    if any(term in query for term in RELATION_TERMS):
        return "relationship", ["relationship_terms"]
    if any(term in query for term in COMPARE_TERMS) and any(term in query for term in ("比较", "对比", "区别", "差异", "不同", "相比")):
        return "comparison", ["comparison_terms"]
    if re.search(r"[\"“”'][^\"“”']+[\"“”']", query):
        return "exact", ["quoted_phrase"]
    return "semantic", ["default_semantic"]


def _channels(purpose: str) -> list[str]:
    mapping = {
        "exact": ["keyword", "vector"], "semantic": ["keyword", "vector"],
        "entity": ["entity", "keyword", "vector"],
        "relationship": ["keyword", "vector", "entity", "graph"],
        "temporal_current": ["keyword", "vector"], "temporal_history": ["keyword", "vector"],
        "comparison_axis": ["keyword", "vector"], "provenance": ["keyword"],
        "inventory": ["keyword"],
    }
    return mapping[purpose]


def build_query_plan(
    query: str,
    profile_name: str,
    profile: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    retrieval_hints: dict[str, Any] | None = None,
    information_needs: list[str] | None = None,
) -> dict[str, Any]:
    original = normalize_query(query)
    require(bool(original), "INVALID_ARGUMENT", "query 不能为空")
    context = context or {}
    resolved, entities = _resolve_context(original, context)
    hint = (retrieval_hints or {}).get("intent")
    intent, triggers = detect_intent(resolved, str(hint) if hint else None)
    allowed = list(profile["allowed_channels"])
    max_branches = int(profile["max_query_branches"])
    max_subqueries = int(profile["max_subqueries"])
    branches: list[dict[str, Any]] = []

    def add(purpose: str, branch_query: str, trigger: str) -> None:
        if len(branches) >= max_branches:
            return
        channels = [channel for channel in _channels(purpose) if channel in allowed]
        branches.append({
            "branch_id": f"b{len(branches) + 1}-{purpose}", "purpose": purpose,
            "query": normalize_query(branch_query), "target_channels": channels, "trigger": trigger,
        })

    if intent == "temporal":
        year_terms = " ".join(re.findall(YEAR_PATTERN, resolved))
        marker_pattern = "|".join(
            sorted(
                (re.escape(term) for term in CURRENT_TERMS + LATER_TERMS + HISTORY_TERMS + TEMPORAL_PAIR_TERMS),
                key=len,
                reverse=True,
            )
        )
        comparison_pattern = r"比较|对比|相比|有(?:没有|无)不同|有什么不同|区别|差异|请把|分开"
        common = re.sub(rf"(?:{marker_pattern}|{comparison_pattern})", " ", resolved)
        current_base = re.sub(YEAR_PATTERN, " ", common)
        later_pattern = "|".join(
            sorted((re.escape(term) for term in CURRENT_TERMS + LATER_TERMS), key=len, reverse=True)
        )
        history_base = re.sub(rf"(?:{later_pattern})", " ", resolved)
        history_base = re.sub(rf"(?:{comparison_pattern}|{'|'.join(re.escape(term) for term in TEMPORAL_PAIR_TERMS)})", " ", history_base)
        add("temporal_current", f"{current_base} 当前 现行", "temporal_pair_terms")
        history_suffix = "" if year_terms else "历史 以前"
        add("temporal_history", f"{history_base} {history_suffix}", "temporal_pair_terms")
    elif intent == "comparison":
        add("comparison_axis", resolved, "comparison_terms")
    else:
        add(intent, resolved, triggers[0])

    needs = [normalize_query(str(item)) for item in (information_needs or []) if normalize_query(str(item))]
    needs = needs[:max_subqueries]
    for need in needs:
        if need != resolved:
            add("semantic", need, "information_need")
    plan_material = {"profile": profile_name, "query": resolved, "intent": intent,
                     "branches": branches, "subqueries": needs, "allowed": allowed}
    plan_id = hashlib.sha256(json.dumps(plan_material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    return {
        "plan_id": f"plan_{plan_id}", "profile": profile_name,
        "original_query": original, "normalized_query": original,
        "resolved_query": resolved, "lexical_query": resolved, "semantic_query": resolved,
        "protected_terms": _protected(resolved, entities), "expansions": [],
        "resolved_context": {"resolved_entities": entities,
                             "time_focus": context.get("time_focus"),
                             "previous_topics": context.get("previous_topics", [])},
        "subqueries": needs, "branches": branches,
        "trace": {"intent": intent, "triggers": triggers, "allowed_channels": allowed,
                  "branch_count": len(branches), "subquery_count": len(needs)},
    }
