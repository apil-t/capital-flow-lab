#!/usr/bin/env python3
"""Run the frozen one-month rank-IC, return-group and top-two diagnostics."""

import argparse
import json
from pathlib import Path

from flowrank.evaluation import evaluate_one_month
from flowrank.storage import connect


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/research_historical.sqlite")
    parser.add_argument("--protocol", type=Path, default=ROOT / "data/pilot/evaluation_protocol.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/generated/one_month_evaluation.json")
    args = parser.parse_args()
    with connect(args.db) as db:
        result = evaluate_one_month(db, args.protocol, args.output)
    print(json.dumps({"decision_dates": len(result["monthly"]),
                      "summary": result["summary"], "report": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
