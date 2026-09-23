#!/usr/bin/env python3
"""Review the pinned NSE action response and mark HDFC pilot ISIN coverage."""

import argparse
import json
import sys
from pathlib import Path

from flowrank.nse_actions import build_action_review
from flowrank.storage import connect


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/research.sqlite")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/nse")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/generated")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/pilot/nse_action_sources.json")
    args = parser.parse_args()
    try:
        with connect(args.db) as db:
            result = build_action_review(db, args.catalog, args.raw_dir, args.output_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps({"reviewed_isins": result["reviewed_isins"],
                      "portfolio_cash_events": result["portfolio_cash_events"],
                      "portfolio_share_changing_events": result["portfolio_share_changing_events"]}, indent=2))


if __name__ == "__main__":
    main()
