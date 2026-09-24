# Capital Flow Lab

Capital Flow Lab is a research tool that ranks Indian stocks using changes in publicly disclosed mutual fund holdings. It compares adjacent monthly portfolios only after their recorded disclosure dates, then can measure later stock returns against a benchmark.

## What it does

- Downloads and validates monthly holdings for five HDFC Mutual Fund equity schemes in the pilot.
- Ranks stocks by how many schemes increased or decreased their portfolio weight or share count.
- Supports disclosure-aware backtests using adjusted stock returns and the Nifty 500 total-return index.

## Try it

Requires Python 3.11 or newer. With [uv](https://docs.astral.sh/uv/) installed:

```bash
bash scripts/setup.sh
source .venv/bin/activate
bash scripts/demo.sh
```

The demo uses synthetic data. To download and rank the real HDFC holdings pilot:

```bash
python scripts/hdfc_pilot.py
flowrank rank --db data/research.sqlite --as-of 2026-09-08 --signal weight
```

## Reproduce the historical one-month test

The pinned public-source pipeline builds a separate database for the 2025–2026 pilot:

```bash
python scripts/hdfc_pilot.py --db data/research_historical.sqlite
python scripts/nse_raw_prices.py --db data/research_historical.sqlite --start 2025-01-01 --end 2026-09-23
python scripts/build_historical_data.py
python scripts/evaluate_one_month.py
```

The test follows the [predefined protocol](data/pilot/evaluation_protocol.json). Its local report is written to `data/generated/one_month_evaluation.md`. Source hashes are pinned; changed public files stop the build for review.

## Current status

The pilot has holdings for January 2025 through August 2026 and 429 trading days of dividend and bonus adjusted returns through September 2026. The predefined test has 18 completed 30-day decision dates. Securities with unsupported actions or missing prices are excluded or skipped, and older disclosure cutoffs are provisional. These exploratory results do **not** establish that the rankings predict future returns. A rising portfolio weight does not necessarily mean a fund bought shares.
