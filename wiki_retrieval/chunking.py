"""Deterministic, heading-aware chunks used by retrieval and indexing."""

from __future__ import annotations

import hashlib
import re
from typing import Any

WIKI_TARGET_CHARS = 1400
WIKI_MAX_CHARS = 2000
WIKI_OVERLAP_CHARS = 160
WIKI_MIN_HEADING_SPLIT_CHARS = 600
SOURCE_TARGET_CHARS = 1600
SOURCE_MAX_CHARS = 1800
SOURCE_OVERLAP_CHARS = 120

_HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
_CLAIM_MARKER = re.compile(r"<!--\s*wiki-claim:.*?-->", re.DOTALL)
_SENTENCE_END = frozenset("。！？.!?；;\n")


def _without_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content
    lines = content.splitlines()
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip()
    return content


def _split_long(text: str, maximum: int, overlap: int) -> list[str]:
    """Prefer sentence boundaries, with a hard character fallback."""
    remaining = text.strip()
    pieces: list[str] = []
    while len(remaining) > maximum:
        floor = max(maximum // 2, maximum - 400)
        cut = max(
            (index + 1 for index, char in enumerate(remaining[:maximum]) if char in _SENTENCE_END),
            default=0,
        )
        if cut < floor:
            cut = maximum
        pieces.append(remaining[:cut].strip())
        remaining = remaining[max(0, cut - overlap) :].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _blocks(content: str, title: str) -> list[tuple[tuple[str, ...], str]]:
    content = _CLAIM_MARKER.sub("", _without_frontmatter(content))
    headings: list[str] = [title]
    blocks: list[tuple[tuple[str, ...], str]] = []
    paragraph: list[str] = []

    def flush() -> None:
        text = "\n".join(paragraph).strip()
        if text:
            blocks.append((tuple(headings), text))
        paragraph.clear()

    for line in content.splitlines():
        match = _HEADING.match(line.strip())
        if match:
            flush()
            level = len(match.group(1))
            depth = max(1, level - 1)
            headings[depth:] = []
            while len(headings) < depth:
                headings.append("")
            headings.append(match.group(2).strip())
            continue
        # The page H1 duplicates the title and is metadata, not evidence text.
        if line.strip().startswith("# "):
            flush()
            continue
        if not line.strip():
            flush()
            continue
        paragraph.append(line.rstrip())
    flush()
    return blocks


def chunk_document(
    *,
    page_id: str,
    title: str,
    content: str,
    scope: str,
) -> list[dict[str, Any]]:
    """Split one document while retaining enough heading context for embedding."""
    if scope == "wiki":
        target, maximum, overlap = WIKI_TARGET_CHARS, WIKI_MAX_CHARS, WIKI_OVERLAP_CHARS
    else:
        target, maximum, overlap = SOURCE_TARGET_CHARS, SOURCE_MAX_CHARS, SOURCE_OVERLAP_CHARS

    units: list[tuple[tuple[str, ...], str]] = []
    for heading_path, block in _blocks(content, title):
        units.extend((heading_path, piece) for piece in _split_long(block, maximum, overlap))

    assembled: list[tuple[tuple[str, ...], str]] = []
    current: list[str] = []
    current_heading: tuple[str, ...] = (title,)
    for heading_path, unit in units:
        proposed = "\n\n".join([*current, unit])
        current_size = len("\n\n".join(current))
        semantic_boundary = (
            scope == "wiki"
            and heading_path != current_heading
            and current_size >= WIKI_MIN_HEADING_SPLIT_CHARS
        )
        if current and (len(proposed) > target or semantic_boundary):
            body = "\n\n".join(current).strip()
            assembled.append((current_heading, body))
            prefix = body[-overlap:].lstrip() if overlap else ""
            current = [prefix, unit] if prefix else [unit]
            current_heading = heading_path
            if len("\n\n".join(current)) > maximum:
                current = [unit]
        else:
            if not current:
                current_heading = heading_path
            current.append(unit)
    if current:
        assembled.append((current_heading, "\n\n".join(current).strip()))

    chunks: list[dict[str, Any]] = []
    occurrences: dict[str, int] = {}
    for ordinal, (heading_path, text) in enumerate(assembled):
        normalized = " ".join(text.split())
        base = hashlib.sha256(
            f"{scope}\0{page_id}\0{' > '.join(heading_path)}\0{normalized}".encode("utf-8")
        ).hexdigest()[:16]
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        chunk_id = f"chk_{base}" if occurrence == 0 else f"chk_{base}_{occurrence}"
        rendered_heading = " > ".join(part for part in heading_path if part)
        chunks.append(
            {
                "chunk_id": chunk_id,
                "page_id": page_id,
                "chunk_index": ordinal,
                "heading_path": rendered_heading,
                "text": text,
                "embedding_text": f"{rendered_heading}\n{text}".strip(),
            }
        )
    return chunks
