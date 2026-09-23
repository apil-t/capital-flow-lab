#!/usr/bin/env python3
"""Build and import the pinned NSE stock and Nifty 500 total-return pilot."""

import argparse
import json
import sys
from pathlib import Path

from flowrank.returns import build_return_prices
from flowrank.storage import connect


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/research.sqlite")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/pilot/return_sources.json")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/nse")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/generated")
    args = parser.parse_args()
    try:
        with connect(args.db) as db:
            result = build_return_prices(db, args.catalog, args.raw_dir, args.output_dir,
                                         args.output_dir / "nse_raw_closes.csv",
                                         args.output_dir / "nse_raw_prices_manifest.json")
    except (OSError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps({"price_rows": result["price_rows"], "stock_isins": result["stock_isins"],
                      "excluded_isins": result["excluded_isins"],
                      "benchmark_id": result["benchmark_id"]}, indent=2))


if __name__ == "__main__":
    main()
