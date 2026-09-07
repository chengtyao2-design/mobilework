"""Prepare the shared 50-page Wiki corpus for external baseline repositories.

The fixture is intentionally derived from the checked-in Wiki pages at run time.
It copies content pages only, preserves their topic-relative paths, and writes a
small merged index.  It does not create hashes or duplicate experiment results.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


EXCLUDED = {"index.md", "log.md"}


def content_pages(source_wiki: Path) -> list[Path]:
    return sorted(path for path in source_wiki.rglob("*.md") if path.name not in EXCLUDED)


def prepare(root: Path, destinations: list[Path]) -> int:
    sources = [root / "kb/kb_enterprise/wiki", root / "kb/kb_research/wiki"]
    pages = [(source, page) for source in sources for page in content_pages(source)]
    if len(pages) != 50:
        raise RuntimeError(f"expected 50 source Wiki pages, found {len(pages)}")

    for destination in destinations:
        index_lines = ["# Knowledge Base Index", "", "Shared 50-page corpus for retrieval comparison.", ""]
        wiki = destination / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        for source, page in pages:
            relative = page.relative_to(source)
            target = wiki / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(page, target)
        for _, page in pages:
            relative = page.relative_to(page.parents[1]).as_posix()
            index_lines.append(f"- [{page.stem}]({relative})")
        (wiki / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
        (wiki / "log.md").write_text("# Wiki Log\n\nFixture prepared for the 2026-09-07 comparison.\n", encoding="utf-8")
        source_skill = destination / "SKILL.md"
        if source_skill.is_file():
            skill_dir = destination / ".opencode/skills/karpathy-llm-wiki"
            skill_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_skill, skill_dir / "SKILL.md")
            source_references = destination / "references"
            if source_references.is_dir():
                shutil.copytree(source_references, skill_dir / "references", dirs_exist_ok=True)
    return len(pages)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("destination", type=Path, nargs="+")
    args = parser.parse_args()
    count = prepare(args.root.resolve(), [path.resolve() for path in args.destination])
    print(f"prepared {count} Wiki pages in {len(args.destination)} baseline repositories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
