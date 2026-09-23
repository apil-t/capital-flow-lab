#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
demo_db="${1:-$project_dir/data/demo.sqlite}"
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m flowrank init --db "$demo_db"
python3 -m flowrank import-holdings --db "$demo_db" --file "$project_dir/data/sample/holdings.csv"
python3 -m flowrank import-prices --db "$demo_db" --file "$project_dir/data/sample/prices.csv"
python3 -m flowrank rank --db "$demo_db" --as-of 2025-04-30
python3 -m flowrank backtest --db "$demo_db" --start 2025-03-31 --end 2025-05-31 --horizon-days 30 --top 2 --benchmark NIFTY500_SYNTH --cost-bps 20
