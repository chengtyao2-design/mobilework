"""Download only the selected local embedding runtime files (no candidate models)."""

from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import hf_hub_download

REPOSITORY = "Xenova/bge-small-zh-v1.5"
FILES = (
    "config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "onnx/model_quantized.onnx",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".mobilework-models/bge-small-zh-v1.5",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for filename in FILES:
        hf_hub_download(REPOSITORY, filename, local_dir=args.output)
        print(f"ready: {filename}")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
