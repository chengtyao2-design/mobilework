from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CLAIM_RE = re.compile(r"<!--\s*wiki-claim:\s*(\{.*?\})\s*-->")
WORD_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)


def _digest(prefix: str, value: str, length: int = 16) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def normalize_identity(value: str) -> str:
    return "".join(WORD_RE.findall(value.casefold()))


def tokenize(value: str) -> list[str]:
    raw = WORD_RE.findall(value.casefold())
    words = [item for item in raw if not (len(item) == 1 and "\u4e00" <= item <= "\u9fff")]
    cjk = [item for item in raw if len(item) == 1 and "\u4e00" <= item <= "\u9fff"]
    return words + cjk + ["".join(cjk[i:i + 2]) for i in range(max(0, len(cjk) - 1))]


def chunk_text(text: str, max_chars: int = 1800) -> list[dict[str, Any]]:
    """Split text into stable content-addressed paragraph chunks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        for piece in (paragraph[i:i + max_chars] for i in range(0, len(paragraph), max_chars)):
            if buffer and len(buffer) + len(piece) + 2 > max_chars:
                chunks.append(buffer)
                buffer = piece
            else:
                buffer = f"{buffer}\n\n{piece}".strip()
    if buffer:
        chunks.append(buffer)
    seen: Counter[str] = Counter()
    result = []
    for ordinal, content in enumerate(chunks):
        sha = hashlib.sha256(content.encode()).hexdigest()
        occurrence = seen[sha]
        seen[sha] += 1
        result.append({
            "chunk_id": _digest("chk_", f"{sha}:{occurrence}"),
            "sha256": sha,
            "ordinal": ordinal,
            "chars": len(content),
            "preview": content[:240],
            "text": content,
        })
    return result


def diff_chunks(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, Any]:
    old_ids = {item["chunk_id"] for item in old}
    new_ids = {item["chunk_id"] for item in new}
    added = [item for item in new if item["chunk_id"] not in old_ids]
    removed = [item for item in old if item["chunk_id"] not in new_ids]
    unchanged = [item for item in new if item["chunk_id"] in old_ids]
    old_chars = sum(item.get("chars", 0) for item in old)
    changed_chars = sum(item.get("chars", 0) for item in added + removed)
    return {
        "added": added, "removed": removed, "unchanged": unchanged,
        "added_count": len(added), "removed_count": len(removed),
        "unchanged_count": len(unchanged), "old_chars": old_chars,
        "new_chars": sum(item.get("chars", 0) for item in new),
        "changed_chars": changed_chars,
        "changed_ratio": round(changed_chars / max(1, old_chars), 6),
    }


@dataclass(frozen=True)
class Claim:
    claim_id: str
    source_ids: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    marker_start: int
    marker_end: int
    block_start: int
    block_end: int


def parse_claims(text: str) -> tuple[Claim, ...]:
    claims: list[Claim] = []
    previous_end = 0
    for match in CLAIM_RE.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise ValueError("invalid wiki-claim JSON") from error
        claim_id = payload.get("id")
        source_ids = payload.get("source_ids")
        chunk_ids = payload.get("chunk_ids", [])
        if not isinstance(claim_id, str) or not claim_id.startswith("clm_"):
            raise ValueError("wiki-claim id must start with clm_")
        if not isinstance(source_ids, list) or not source_ids or not all(isinstance(x, str) for x in source_ids):
            raise ValueError(f"wiki-claim {claim_id} needs non-empty source_ids")
        if not isinstance(chunk_ids, list) or not all(isinstance(x, str) for x in chunk_ids):
            raise ValueError(f"wiki-claim {claim_id} has invalid chunk_ids")
        prefix = text[previous_end:match.start()]
        stripped_end = len(prefix.rstrip())
        separator = prefix.rfind("\n\n", 0, stripped_end)
        claims.append(Claim(
            claim_id, tuple(dict.fromkeys(source_ids)), tuple(dict.fromkeys(chunk_ids)),
            match.start(), match.end(), previous_end + (separator + 2 if separator >= 0 else 0),
            previous_end + stripped_end,
        ))
        previous_end = match.end()
    return tuple(claims)


def claim_marker(claim_id: str, source_ids: Iterable[str], chunk_ids: Iterable[str]) -> str:
    payload = {"id": claim_id, "source_ids": list(dict.fromkeys(source_ids)), "chunk_ids": list(dict.fromkeys(chunk_ids))}
    return f"<!-- wiki-claim: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))} -->"


def prune_claims(text: str, deleted_source_ids: set[str]) -> tuple[str, dict[str, Any]]:
    """Remove exclusively-backed claim blocks and prune IDs from shared claims."""
    claims = parse_claims(text)
    edits: list[tuple[int, int, str]] = []
    removed: list[str] = []
    retained: list[str] = []
    for claim in claims:
        survivors = [item for item in claim.source_ids if item not in deleted_source_ids]
        if len(survivors) == len(claim.source_ids):
            continue
        if survivors:
            edits.append((claim.marker_start, claim.marker_end, claim_marker(claim.claim_id, survivors, claim.chunk_ids)))
            retained.append(claim.claim_id)
        else:
            start, end = claim.block_start, claim.marker_end
            while start > 0 and text[start - 1] == "\n":
                start -= 1
            while end < len(text) and text[end] == "\n":
                end += 1
            edits.append((start, end, ""))
            removed.append(claim.claim_id)
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, {"removed_claims": removed, "pruned_claims": retained, "claim_count": len(claims)}


def bm25_candidates(query: str, documents: list[dict[str, Any]], top_k: int = 8) -> list[dict[str, Any]]:
    """Rank existing pages as merge candidates without an embedding service."""
    query_tokens = tokenize(query)
    if not query_tokens or not documents:
        return []
    doc_tokens = [tokenize(item.get("text", "")) for item in documents]
    avgdl = sum(map(len, doc_tokens)) / len(doc_tokens)
    df = Counter(token for tokens in doc_tokens for token in set(tokens))
    query_identity = normalize_identity(query[:200])
    ranked = []
    for document, tokens in zip(documents, doc_tokens):
        counts, score = Counter(tokens), 0.0
        for token in query_tokens:
            freq = counts.get(token, 0)
            if freq:
                idf = math.log(1 + (len(documents) - df[token] + 0.5) / (df[token] + 0.5))
                score += idf * (freq * 2.2) / (freq + 1.2 * (0.25 + 0.75 * len(tokens) / max(1, avgdl)))
        identities = [document.get("title", ""), *document.get("aliases", [])]
        exact = any(normalize_identity(item) == query_identity for item in identities if item)
        score += 1000.0 if exact else 0.0
        if score > 0:
            ranked.append({"page": document["page"], "title": document.get("title", ""),
                           "score": round(score, 6), "exact_identity": exact,
                           "reason": "exact title/alias" if exact else "BM25 lexical overlap"})
    return sorted(ranked, key=lambda item: (-item["score"], item["page"]))[:top_k]


def page_documents(wiki: Path) -> list[dict[str, Any]]:
    from .frontmatter import parse_managed_page
    result = []
    for path in sorted(wiki.rglob("*.md")):
        rel = path.relative_to(wiki).as_posix()
        if rel in {"index.md", "log.md"}:
            continue
        try:
            page = parse_managed_page(path)
        except ValueError:
            continue
        if page.managed:
            result.append({"page": rel, "title": page.title, "aliases": list(page.aliases),
                           "text": " ".join([page.title, *page.aliases, page.summary])})
    return result
