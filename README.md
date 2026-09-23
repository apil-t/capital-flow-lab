# Capital Flow Lab

An initial research project for the idea in the [shared conversation](https://chatgpt.com/share/6ab4194c-72fc-83ee-81c8-e5b7dcb33d34): test whether **publicly disclosed changes in Indian mutual-fund holdings** contain useful information about later stock returns. It ranks stocks by the breadth of scheme-level portfolio-weight increases, then measures later returns against a benchmark.

The included data is entirely **synthetic**. The demo output says nothing about a real stock or strategy.

## Run the demo

Requires Python 3.11 or newer. No external Python packages are needed for the demo.

```bash
cd capital-flow-lab
bash scripts/demo.sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The demo creates `data/demo.sqlite` and shows one ranking plus three monthly research cohorts. Re-running it is safe with the same sample files. You can also install the command with `python3 -m pip install -e .` and use `flowrank` instead of `PYTHONPATH=src python3 -m flowrank`.

## How this first version works

1. Import a **complete** portfolio snapshot for each fund scheme and reporting period. Each snapshot has a period end, an actual publication timestamp with timezone, and a source URL.
2. At an `--as-of` date, select only snapshots already public by the end of that date in India. A later correction stays invisible to earlier decisions.
3. Compare the latest two visible snapshots within each scheme. A weight increase of more than 0.01 percentage points counts as one increasing scheme; a decrease counts as one decreasing scheme.
4. Rank by `increasing_schemes - decreasing_schemes + clipped(net_weight_change_pp / 2, -1, 1)`. This is a simple hypothesis to test, not an optimized or predictive model.
5. For each month-end decision, take the top `N`; enter at the first available adjusted close **after** the decision date; exit on the first available close at least `horizon_days` later; compare with the benchmark on the exact same dates; subtract a configurable round-trip cost.

An incomplete cohort is skipped. The reported average is an average of independent cohorts, **not** a compounded portfolio return.

## Input contracts

`import-holdings` expects exactly these CSV columns:

| Column | Meaning |
| --- | --- |
| `scheme_id` | Stable scheme identifier; use the same ID across months |
| `asset_id` | Stable security identifier, preferably an ISIN for real equities |
| `period_end` | `YYYY-MM-DD` date the holdings describe |
| `published_at` | ISO 8601 actual public timestamp, including timezone offset |
| `weight_pct` | Portfolio weight in percentage units (for example, `2.5`) |
| `source_url` | URL of the original disclosure or archive |

Each `(scheme_id, period_end, published_at)` in an import file must represent the **whole relevant equity portfolio**, because an omitted security is interpreted as a zero weight. Use a new publication timestamp for a corrected filing; never overwrite historical knowledge.

`import-prices` expects `asset_id,price_date,adjusted_close,source_url`. Prices must be adjusted consistently for splits, dividends and other corporate actions, including the benchmark series. Use historical constituents when expanding the investable universe. Never treat a current stock list as the past universe.

Example commands after preparing your own CSVs:

```bash
PYTHONPATH=src python3 -m flowrank init --db data/research.sqlite
PYTHONPATH=src python3 -m flowrank import-holdings --db data/research.sqlite --file path/to/holdings.csv
PYTHONPATH=src python3 -m flowrank import-prices --db data/research.sqlite --file path/to/prices.csv
PYTHONPATH=src python3 -m flowrank rank --db data/research.sqlite --as-of 2025-04-30
PYTHONPATH=src python3 -m flowrank backtest --db data/research.sqlite --start 2020-01-01 --end 2025-12-31 --horizon-days 90 --top 20 --benchmark YOUR_BENCHMARK_ID --cost-bps 25
```

## What to build next

1. **Source adapters and archive:** start with a small set of mutual-fund schemes. Download and retain each original disclosure, its discovery/publication timestamp, checksum, and parser version. [AMFI has a portfolio disclosure portal](https://www.amfiindia.com/online-center/portfolio-disclosure); verify each fund's actual release date and download terms.
2. **Identifiers and corporate actions:** map scheme names and company names to stable IDs; distinguish new positions, mergers, renames and stock splits. Portfolio weight changes can result from price movement or fund cash flows, so they are *allocation proxies*, not proof of buying. Add split-adjusted share quantities when available.
3. **Research controls:** build historical Nifty 500 membership, delisted-stock coverage, liquidity filters, market-cap/sector baselines, and multiple horizons. Track missing data instead of silently dropping it. Evaluate on later untouched years after choosing rules on earlier years.
4. **Additional signals:** add published [NSE shareholding filings](https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern), financial statements, insider filings and adjusted market data. Keep every feature tied to when it became public.
5. **Model only if warranted:** compare any ML ranker with this frozen rules baseline on truly out-of-sample data. A more complex model is useful only if it improves the result after costs and realistic coverage rules.

### Current limits

This is a research scaffold, not an investment recommendation or live trading system. It has no real-data downloader, no corporate-action processor, no survivorship-safe historical universe, no tax model, and no executable portfolio simulation. The synthetic demo cannot validate the investment hypothesis. The first meaningful milestone is a correctly timestamped real-data archive and a preregistered historical test.
