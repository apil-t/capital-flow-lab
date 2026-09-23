#!/usr/bin/env python3
"""Archive official NSE daily raw closes for imported HDFC ISINs."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from flowrank.nse_prices import archive_raw_closes
from flowrank.storage import connect


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data/research.sqlite")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/nse/bhavcopy")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/generated")
    args = parser.parse_args()
    try:
        with connect(args.db) as db:
            result = archive_raw_closes(db, date.fromisoformat(args.start), date.fromisoformat(args.end),
                                        args.raw_dir, args.output_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps({"trading_days": len(result["days"]), "rows": result["rows"],
                      "unavailable_dates": result["unavailable_dates"],
                      "price_basis": result["price_basis"]}, indent=2))


if __name__ == "__main__":
    main()
