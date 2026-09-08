"""Publish an already reviewed, staged research batch through the normal CLI."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    kb = args.root / "kb/kb_research"
    analysis = json.loads((kb / ".wiki-state/batches" / args.batch / "semantic-analysis.json").read_text(encoding="utf-8"))
    cli = [str(Path(sys.executable).with_name("wiki.exe")), "--root", str(args.root), "--kb", "kb_research"]
    for source in analysis["sources"]:
        pages = [p["path"] for p in source["page_plan"] if p["action"] != "skip"]
        subprocess.run([*cli, "record-source", "--batch", args.batch, "--source-id", source["source_id"], "--pages-json", json.dumps(pages)], check=True)
    subprocess.run([*cli, "commit", "--batch", args.batch, "--no-reindex"], check=True)


if __name__ == "__main__":
    main()
