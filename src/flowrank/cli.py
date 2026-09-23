"""Command-line interface for the first research baseline."""

import argparse
import json
import sys
from datetime import date

from .research import backtest, rank
from .storage import connect, import_holdings, import_prices


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="flowrank", description="Research disclosed fund allocation changes")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "import-holdings", "import-prices", "rank", "backtest"):
        sub = commands.add_parser(name)
        sub.add_argument("--db", required=True, help="SQLite database path")
        if name.startswith("import-"):
            sub.add_argument("--file", required=True)
        if name == "rank":
            sub.add_argument("--as-of", required=True, help="YYYY-MM-DD in India time")
        if name == "backtest":
            sub.add_argument("--start", required=True)
            sub.add_argument("--end", required=True)
            sub.add_argument("--horizon-days", type=int, default=30)
            sub.add_argument("--top", type=int, default=2)
            sub.add_argument("--benchmark", required=True, help="Asset ID for an adjusted benchmark series")
            sub.add_argument("--cost-bps", type=float, default=20, help="Estimated round-trip cost")
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        with connect(args.db) as db:
            if args.command == "init":
                print(f"Initialized {args.db}")
            elif args.command == "import-holdings":
                print(f"Imported {import_holdings(db, args.file)} complete fund snapshots")
            elif args.command == "import-prices":
                print(f"Imported {import_prices(db, args.file)} adjusted prices")
            elif args.command == "rank":
                scores = rank(db, date.fromisoformat(args.as_of))
                print("asset_id\tscore\tup\tdown\tnew\tnet_weight_change_pp")
                for row in scores:
                    print(
                        f"{row.asset_id}\t{row.score:.3f}\t{row.increasing_schemes}"
                        f"\t{row.decreasing_schemes}\t{row.new_positions}"
                        f"\t{row.net_weight_change_pp:.3f}"
                    )
            elif args.command == "backtest":
                results = backtest(
                    db, date.fromisoformat(args.start), date.fromisoformat(args.end),
                    args.horizon_days, args.top, args.benchmark, args.cost_bps,
                )
                for row in results:
                    print(json.dumps(row, sort_keys=True))
                evaluated = [row for row in results if row["status"] == "evaluated"]
                summary = {
                    "cohorts_evaluated": len(evaluated),
                    "cohorts_skipped": len(results) - len(evaluated),
                    "mean_net_excess_return": (
                        sum(row["mean_net_excess_return"] for row in evaluated) / len(evaluated)
                        if evaluated else None
                    ),
                }
                print(json.dumps({"summary": summary}, sort_keys=True))
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
