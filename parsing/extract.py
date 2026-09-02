"""Bridge parser-owned originals into mobilework's raw Wiki boundary."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORIGINALS = PROJECT_ROOT / "parsing" / "originals"
DEFAULT_STATE = PROJECT_ROOT / "raw" / ".llmwiki" / ".document_parser"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="解析 parsing/originals，并将质量门通过的结果发布到 raw/。")
    parser.add_argument("--document-parser-root", type=Path, default=None)
    parser.add_argument("--originals", type=Path, default=DEFAULT_ORIGINALS)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--parser", default=None)
    parser.add_argument("--options-json", default=None)
    return parser.parse_args(argv)


def resolve_parser_root(explicit: Path | None) -> Path | None:
    configured = explicit or (Path(os.environ["DOCUMENT_PARSER_ROOT"]) if os.environ.get("DOCUMENT_PARSER_ROOT") else None)
    if configured is None:
        sibling = PROJECT_ROOT.parents[1]
        configured = sibling if (sibling / "app" / "bootstrap.py").is_file() else None
    return configured.resolve() if configured else None


def load_lifecycle_builder(parser_root: Path | None) -> Callable:
    if parser_root is not None:
        if not (parser_root / "app" / "bootstrap.py").is_file():
            raise RuntimeError(f"不是有效的 document_parser 仓库：{parser_root}")
        parent = str(parser_root.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
    try:
        from document_parser.app.bootstrap import build_source_lifecycle
    except ModuleNotFoundError as error:
        raise RuntimeError("找不到 document_parser；请传 --document-parser-root 或设置 DOCUMENT_PARSER_ROOT。") from error
    return build_source_lifecycle


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        options = json.loads(args.options_json) if args.options_json else {}
        if not isinstance(options, dict):
            raise ValueError("--options-json 必须是 JSON object")
        builder = load_lifecycle_builder(resolve_parser_root(args.document_parser_root))
        output = args.output_dir or args.state_dir / "packages"
        lifecycle = builder(raw_root=args.originals, state_root=args.state_dir, storage_root=output, mobilework_root=PROJECT_ROOT)
        events = lifecycle.scan(parser_id=args.parser, options=options)
    except Exception as error:
        print(f"解析接入失败：{error}", file=sys.stderr)
        return 2
    for event in events:
        print(json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True))
    return 1 if any(event.error for event in events) else 0


if __name__ == "__main__":
    raise SystemExit(main())
