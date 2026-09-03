# 分词、短语归一、片段截取
"""Pure text helpers shared by the retrieval channels.

Two tokenizers live here on purpose: `tokenize_query` is fine-grained (CJK
bigrams plus single chars) and drives keyword scoring, while `split_terms` is
deliberately coarser and drives entity/source matching. Merging them collapses
recall on one side or precision on the other.
"""

from __future__ import annotations

from pathlib import Path

SPLIT_CHARS = frozenset(",，。！？、；;:\"'‘’“”（）()-_/\\·~～…[]{}")

STOP_WORDS = frozenset(
    {
        "的", "是", "了", "在", "有", "和", "与", "对", "为",
        "the", "is", "a", "an", "what", "how", "are", "was", "were",
        "do", "does", "did", "be", "been", "being", "have", "has", "had",
        "it", "its", "in", "on", "at", "to", "for", "of", "with", "by",
        "this", "that", "these", "those",
    }
)

TRIM_CHARS = frozenset("，。！？、；;,:：\"'‘’“”（）()[]{}")

TERM_SPLIT_CHARS = frozenset(",，;；:：")
TERM_STOP_WORDS = frozenset(
    {"raw", "source", "sources", "file", "files", "原始资料", "原始文件", "源文件"}
)

SNIPPET_CONTEXT = 80
MAX_SNIPPET = 320


def _split(text: str, delimiters: frozenset[str]) -> list[str]:
    """Split on whitespace or any delimiter. A character loop avoids having to
    escape the regex metacharacters that both delimiter sets contain."""
    parts: list[str] = []
    buffer: list[str] = []
    for char in text:
        if char.isspace() or char in delimiters:
            parts.append("".join(buffer))
            buffer.clear()
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return parts


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF


def tokenize_query(query: str) -> list[str]:
    """CJK tokens longer than 2 chars also yield every adjacent bigram and every
    single char, so Chinese queries match without a segmenter."""
    out: list[str] = []
    for raw in _split(query.lower(), SPLIT_CHARS):
        token = raw.strip()
        if not token or token in STOP_WORDS:
            continue
        chars = list(token)
        if len(chars) <= 1:
            continue
        if any(_is_cjk(char) for char in chars) and len(chars) > 2:
            for index in range(len(chars) - 1):
                out.append(chars[index] + chars[index + 1])
            out.extend(char for char in chars if char not in STOP_WORDS)
            out.append(token)
        else:
            out.append(token)
    return sorted(set(out))


def split_terms(query: str) -> list[str]:
    """Coarser than tokenize_query: no bigrams, no single chars."""
    return [
        term
        for term in (part.strip() for part in _split(query.lower(), TERM_SPLIT_CHARS))
        if len(term) >= 2 and term not in TERM_STOP_WORDS
    ]


def trim_punctuation(text: str) -> str:
    return text.strip().strip("".join(TRIM_CHARS) + " \t\r\n")


def page_id(path: str) -> str:
    """The file stem doubles as the stable page identity."""
    return Path(path).stem


def norm_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lower()


def alias(raw: str) -> str:
    """Collapses a wikilink target or page path to a lookup key."""
    head = raw.split("#", 1)[0].strip()
    if head.endswith(".md"):
        head = head[: -len(".md")]
    return head.replace("\\", "/").replace(" ", "-").lower()


def extract_title(content: str, path: Path) -> str:
    """Frontmatter title, then first H1, then the file stem."""
    if content.startswith("---"):
        in_frontmatter = False
        for line in content.splitlines():
            if line.strip() == "---":
                if in_frontmatter:
                    break
                in_frontmatter = True
                continue
            if in_frontmatter and line.startswith("title:"):
                return line[len("title:") :].strip().strip("\"'")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            heading = stripped[2:].strip()
            if heading:
                return heading
    return path.stem or "Untitled"


def frontmatter_value(content: str, key: str) -> str | None:
    """Only scans the leading frontmatter block."""
    if not content.startswith("---"):
        return None
    for line in content.splitlines()[1:]:
        if line.strip() == "---":
            break
        prefix = f"{key}:"
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip("\"'")
    return None


def extract_links(content: str) -> list[str]:
    """Takes the target side of [[target|label#anchor]]."""
    out: list[str] = []
    rest = content
    while True:
        start = rest.find("[[")
        if start == -1:
            break
        rest = rest[start + 2 :]
        end = rest.find("]]")
        if end == -1:
            break
        target = rest[:end].split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            out.append(target)
        rest = rest[end + 2 :]
    return out


def _window(text: str, position: int, before: int, after: int) -> str:
    start = max(0, position - before)
    return text[start : position + after]


def snippet(content: str, anchor: str) -> str:
    """Drops frontmatter/heading lines, then centres on the anchor."""
    plain = " ".join(
        line
        for line in content.splitlines()
        if not line.strip().startswith("---") and not line.strip().startswith("#")
    )
    lowered = plain.lower()
    position = lowered.find(anchor.lower())
    if position == -1:
        for token in tokenize_query(anchor):
            position = lowered.find(token)
            if position != -1:
                break
    if position == -1:
        position = 0
    # The anchor is located in the lowercased copy but sliced out of the original
    # to keep casing; that only stays aligned while case folding preserves length.
    source = plain if len(lowered) == len(plain) else lowered
    return _window(source, position, SNIPPET_CONTEXT, SNIPPET_CONTEXT * 2).strip()


def wide_snippet(content: str, position: int) -> str:
    """Wider window than `snippet`, and keeps the original casing."""
    window = _window(content, position, MAX_SNIPPET // 2, MAX_SNIPPET)
    return window.replace("\r", " ").replace("\n", " ").strip()
