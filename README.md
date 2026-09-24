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

## Current status

The pilot has holdings for January 2025 through August 2026, but its adjusted return data covers only August–September 2026. It has just one completed 30-day comparison, so its rankings have **not** been shown to predict future returns. Older disclosure cutoffs are provisional, and a rising portfolio weight does not necessarily mean a fund bought shares.
