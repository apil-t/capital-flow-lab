"""Command-line interface for the first research baseline."""

import argparse
import json
import sys
from datetime import date

from .research import backtest, quantity_rank, rank
from .storage import connect, import_action_reviews, import_holdings, import_prices, import_share_actions


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="flowrank", description="Research disclosed fund allocation changes")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "import-holdings", "import-prices", "import-actions",
                 "import-action-reviews", "rank", "backtest", "compare"):
        sub = commands.add_parser(name)
        sub.add_argument("--db", required=True, help="SQLite database path")
        if name.startswith("import-"):
            sub.add_argument("--file", required=True)
        if name == "rank":
            sub.add_argument("--as-of", required=True, help="YYYY-MM-DD in India time")
            sub.add_argument("--signal", choices=("weight", "quantity"), default="weight")
            sub.add_argument("--allow-unreviewed-actions", action="store_true",
                             help="Exploratory quantity ranking without verified corporate-action coverage")
        if name in ("backtest", "compare"):
            sub.add_argument("--start", required=True)
            sub.add_argument("--end", required=True)
            sub.add_argument("--horizon-days", type=int, default=30)
            sub.add_argument("--top", type=int, default=2)
            sub.add_argument("--benchmark", required=True, help="Asset ID for an adjusted benchmark series")
            sub.add_argument("--cost-bps", type=float, default=20, help="Estimated round-trip cost")
            sub.add_argument("--schedule", choices=("month_end", "disclosure"),
                             default="month_end" if name == "backtest" else "disclosure")
            sub.add_argument("--allow-unreviewed-actions", action="store_true")
            if name == "backtest":
                sub.add_argument("--signal", choices=("weight", "quantity"), default="weight")
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
            elif args.command == "import-actions":
                print(f"Imported {import_share_actions(db, args.file)} share actions")
            elif args.command == "import-action-reviews":
                print(f"Imported {import_action_reviews(db, args.file)} action-review intervals")
            elif args.command == "rank":
                if args.signal == "weight":
                    scores = rank(db, date.fromisoformat(args.as_of))
                    print("asset_id\tscore\tup\tdown\tnew\tnet_weight_change_pp")
                    for row in scores:
                        print(f"{row.asset_id}\t{row.score:.3f}\t{row.increasing_schemes}"
                              f"\t{row.decreasing_schemes}\t{row.new_positions}"
                              f"\t{row.net_weight_change_pp:.3f}")
                else:
                    scores = quantity_rank(db, date.fromisoformat(args.as_of), args.allow_unreviewed_actions)
                    print("asset_id\tscore\tup\tdown\tnew\tnet_share_change\tunreviewed_pairs")
                    for row in scores:
                        print(f"{row.asset_id}\t{row.score}\t{row.increasing_schemes}"
                              f"\t{row.decreasing_schemes}\t{row.new_positions}"
                              f"\t{row.net_share_change:.0f}\t{row.unreviewed_pairs}")
                    if args.allow_unreviewed_actions:
                        print("Exploratory only: corporate-action coverage is unverified", file=sys.stderr)
                    elif not scores:
                        print("No reviewed quantity comparisons; import action-review coverage first", file=sys.stderr)
            elif args.command == "backtest":
                results = backtest(
                    db, date.fromisoformat(args.start), date.fromisoformat(args.end),
                    args.horizon_days, args.top, args.benchmark, args.cost_bps,
                    args.signal, args.schedule, args.allow_unreviewed_actions,
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
            elif args.command == "compare":
                options = (db, date.fromisoformat(args.start), date.fromisoformat(args.end),
                           args.horizon_days, args.top, args.benchmark, args.cost_bps)
                weight = backtest(*options, signal="weight", schedule=args.schedule)
                quantity = backtest(*options, signal="quantity", schedule=args.schedule,
                                    allow_unreviewed_actions=args.allow_unreviewed_actions)
                by_date = {row["decision_date"]: row for row in quantity}
                paired = [(row, by_date[row["decision_date"]]) for row in weight
                          if row["status"] == "evaluated" and by_date[row["decision_date"]]["status"] == "evaluated"]
                print(json.dumps({
                    "schedule": args.schedule,
                    "quantity_action_review": "unverified" if args.allow_unreviewed_actions else "required",
                    "weight_evaluated": sum(row["status"] == "evaluated" for row in weight),
                    "quantity_evaluated": sum(row["status"] == "evaluated" for row in quantity),
                    "paired_evaluated": len(paired),
                    "mean_paired_quantity_minus_weight_excess_return": (
                        sum(q["mean_net_excess_return"] - w["mean_net_excess_return"]
                            for w, q in paired) / len(paired) if paired else None),
                    "weight_skips": [row for row in weight if row["status"] == "skipped"],
                    "quantity_skips": [row for row in quantity if row["status"] == "skipped"],
                }, sort_keys=True, indent=2))
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
