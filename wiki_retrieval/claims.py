"""Provenance metadata and bounded, temporal-intent-only freshness ranking."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def temporal_intent(query: str) -> bool:
    return bool(re.search(r"最新|目前|当前|现行|变化|冲突|早期|近期|\b(latest|current|recent|changed|conflict)\b|20\d\d", query, re.I))


def enrich(entry: dict, root: Path, query: str, mode: str) -> None:
    if mode == "off":
        return
    manifest_path = root / ".wiki-state/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    pages = manifest.get("pages", {})
    page = pages.get(entry["path"], pages.get(entry["path"].removeprefix("wiki/"), {}))
    ids = {identifier for chunk in entry.get("matched_chunks", []) for identifier in chunk.get("claim_ids", [])}
    claims = []
    for raw in page.get("claims", []):
        if raw.get("id", raw.get("claim_id")) not in ids:
            continue
        claim = dict(raw)
        claim["claim_id"] = claim.get("claim_id", claim.get("id"))
        claim["source_chunk_ids"] = claim.get("source_chunk_ids", claim.get("chunk_ids", []))
        claim["source_count"] = len(set(claim.get("source_ids", [])))
        claim.setdefault("verification_status", "unverified")
        from .loader import source_metadata
        source_details = []
        for source_id in claim.get("source_ids", []):
            source = manifest.get("sources", {}).get(source_id, {})
            source_path = (root / source.get("path", "")).resolve()
            if source_path.is_relative_to(root.resolve()) and source_path.is_file():
                source_details.append({"source_id": source_id, **source_metadata(source_path.read_text(encoding="utf-8"))})
        claim["source_metadata"] = source_details
        # Preserve differing source dates separately; only inherit common dates.
        for key in ("source_updated_at", "source_published_at", "effective_from", "effective_to"):
            dates = {source[key] for source in source_details if source.get(key)}
            if not claim.get(key) and len(dates) == 1:
                claim[key] = next(iter(dates))
        for key in ("compiled_at", "source_updated_at", "source_published_at", "effective_from", "effective_to", "confidence"):
            claim.setdefault(key, None)
        claim.setdefault("supersedes", [])
        claim.setdefault("contradicts", [])
        claims.append(claim)
    if mode in ("context", "both"):
        entry["matched_claims"] = claims
        entry["verification_status"] = "unverified" if not claims else "claim_metadata_available"
    if mode in ("rerank", "both") and temporal_intent(query) and claims:
        qualities = []
        for claim in claims:
            freshness = .5
            stamp = claim.get("source_updated_at") or claim.get("source_published_at")
            if stamp:
                try:
                    stamp = str(stamp)
                    if re.fullmatch(r"\d{4}", stamp):
                        stamp += "-01-01"
                    elif re.fullmatch(r"\d{4}-\d{2}", stamp):
                        stamp += "-01"
                    date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    days = max(0, (datetime.now(timezone.utc) - date).days)
                    freshness = 1 / (1 + days / 365)
                except (ValueError, TypeError):
                    pass
            verified = 1 if claim["verification_status"] == "verified" else 0
            qualities.append(.5 * verified + .25 * min(1, claim["source_count"] / 3) + .25 * freshness)
        factor = min(1.1, max(.9, .9 + .2 * max(qualities)))
        entry["freshness_factor"] = factor
        entry["fused_score"] = entry["global_score"] * factor
