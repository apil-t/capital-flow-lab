#!/usr/bin/env python3
"""Import pinned 2025-2026 action reviews and adjusted NSE return series."""

import argparse
import json
from pathlib import Path

from flowrank.historical import build_historical_action_reviews, build_historical_prices
from flowrank.storage import connect


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/research_historical.sqlite")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/pilot/historical_sources.json")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/nse")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/generated")
    args = parser.parse_args()
    raw_csv = args.output_dir / "nse_raw_closes.csv"
    raw_manifest = args.output_dir / "nse_raw_prices_manifest.json"
    with connect(args.db) as db:
        reviews = build_historical_action_reviews(db, args.catalog, args.raw_dir,
                                                  args.output_dir, raw_csv, raw_manifest)
        prices = build_historical_prices(db, args.catalog, args.raw_dir, args.output_dir,
                                         raw_csv, raw_manifest)
    print(json.dumps({"reviewed_asset_months": reviews["reviewed_asset_months"],
                      "bonus_adjustments": reviews["bonus_adjustments"],
                      "trading_days": prices["trading_days"],
                      "stock_isins": prices["stock_isins"],
                      "excluded_isins": len(prices["excluded_isins"])}, indent=2))


if __name__ == "__main__":
    main()
