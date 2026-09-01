from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_SOURCES = REPO_ROOT / "raw" / "sources"
WIKI_CATEGORIES = ("concepts", "entities", "references", "skills", "sources", "synthesis")


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Throwaway wiki project root seeded with the real raw/sources corpus.

    Mirrors the on-disk layout (raw/sources plus empty wiki/<category>/) so the
    build engine and the retrieval loader can run against the realistic corpus
    without touching the checked-out tree.
    """
    root = tmp_path / "project"
    sources = root / "raw" / "sources"
    sources.mkdir(parents=True)
    for md in sorted(RAW_SOURCES.glob("*.md")):
        shutil.copy2(md, sources / md.name)
    for category in WIKI_CATEGORIES:
        (root / "wiki" / category).mkdir(parents=True)
    return root
