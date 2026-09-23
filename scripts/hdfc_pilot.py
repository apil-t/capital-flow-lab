#!/usr/bin/env python3
"""Fetch and load the pinned three-month HDFC pilot."""

import argparse
import json
import sys
from pathlib import Path

from flowrank.hdfc import build_pilot
from flowrank.storage import connect


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/research.sqlite")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/hdfc")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/generated")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/pilot/hdfc_sources.json")
    args = parser.parse_args()
    try:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with connect(args.db) as db:
            result = build_pilot(db, args.catalog, args.raw_dir, args.output_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps({"snapshots": result["snapshot_count"],
                      "holdings": result["holdings_count"],
                      "manifest": str(args.output_dir / "hdfc_manifest.json")}, indent=2))


if __name__ == "__main__":
    main()
