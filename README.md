# Capital Flow Lab

Capital Flow Lab ranks Indian stocks using publicly disclosed changes in mutual-fund portfolios. It compares scheme-level portfolio weights and share counts using recorded disclosure cutoffs for each decision date, then measures subsequent stock returns against a benchmark. Older cutoffs are provisional where an original notice has not been verified.

The demo data is **synthetic**. A separate, reproducible pilot imports real HDFC Mutual Fund disclosures and a short window of public return data. Neither output establishes that the rankings predict returns.

## Set up with uv

With [uv](https://docs.astral.sh/uv/) installed, create `.venv` and install the project (editable, with the pilot extra):

```bash
bash scripts/setup.sh
source .venv/bin/activate
```

This puts `flowrank` on your `PATH`. Re-running is safe. All commands below assume the virtual environment is active and you are in the project root.

Without uv, use Python 3.11 or newer: `python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[pilot]'`.

## Run the demo

```bash
bash scripts/demo.sh
python -m unittest discover -s tests -v
```

The demo creates `data/demo.sqlite` and shows one ranking plus three monthly research cohorts. Re-running it is safe with the same sample files.

## Run the HDFC disclosure pilot

The pilot covers [five HDFC equity schemes](https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio) for every month from January 2025 through August 2026. The source catalog pins 100 original XLSX files and three [monthly disclosure notices](https://www.hdfcfund.com/statutory-disclosure/portfolio/notices-portfolio) with URLs and SHA-256 hashes. The parser checks each workbook's scheme, reporting date, equity layout, and subtotal. It imports 100 listed-equity snapshots (5,858 holding rows) with ISIN, reported name, weight, and share quantity. Rankings require adjacent reporting months.

The setup script already installs the spreadsheet reader the pilot needs. Run:

```bash
python scripts/hdfc_pilot.py
python scripts/nse_action_review.py
python scripts/nse_raw_prices.py --start 2026-08-01 --end 2026-09-23
python scripts/build_return_prices.py
flowrank rank --db data/research.sqlite --as-of 2026-09-08 --signal weight
flowrank rank --db data/research.sqlite --as-of 2026-09-08 --signal quantity
flowrank compare --db data/research.sqlite --start 2026-08-01 --end 2026-09-23 --schedule disclosure --horizon-days 30 --top 2 --benchmark NIFTY500_TRI
```

To discover additional historical files, give an inclusive range of reporting months, then rebuild the local database:

```bash
python scripts/discover_hdfc_history.py --start 2025-01 --end 2026-05
python scripts/hdfc_pilot.py
```

The discovery command uses exact links recorded from HDFC's monthly portfolio page for June–October 2025, when two renamed schemes had unusual filenames. For other months it tries HDFC's usual file pattern and older scheme names. It adds a month to `data/pilot/hdfc_sources.json` only when all five downloaded workbooks parse and reconcile. It records missing files or changed layouts in `data/generated/hdfc_discovery_report.json`. A rerun skips pinned months. HDFC used a `@` marker for weights below 0.01% in some older files; the importer retains the share quantity and records a zero weight for these rows. It also accepts `IN9` ISINs for partly paid equity shares.

The pinned June–August 2026 notices are dated July 8, August 8, and September 8. Those snapshots become available at 23:59:59 India time on the later of the notice date and observed HTTP `Last-Modified` date. For older months without a verified notice, the cutoff is the later of the workbook's observed HTTP `Last-Modified` date and ten calendar days after month end. **That older cutoff is an unverified research proxy**, not proof of when the file first became public. For example, March 2025 snapshots become visible April 24 because their current workbooks have that `Last-Modified` date; this may reflect a later replacement. The generated manifest marks each snapshot's availability basis. A September 7 rank compares June → July; a September 8 rank compares July → August.

The weight signal counts schemes whose portfolio weight rose. The separate quantity signal counts schemes whose reported share count rose after applying imported share multipliers. Both use scheme breadth as the primary score. A quantity ranking requires documented corporate-action review coverage by default. The pinned [NSE corporate-action review](https://www.nseindia.com/companies-listing/corporate-filings-actions) covers the original June–August 2026 comparison set's 229 NSE-listed ISINs from July 1 through August 31, 2026: 79 matching records were cash dividends and none was classified as share-changing. Older periods do not yet have action-review coverage, so strict quantity rankings there are empty. For exploratory quantity rankings without coverage, add `--allow-unreviewed-actions`; its output reports `unreviewed_pairs`.

These signals differ substantially: on September 8, Eternal (`INE758T01015`) has four increasing schemes by weight but zero by quantity, two decreasing schemes by quantity, and a net reported decrease of 7,820,000 shares. A higher weight alone is not evidence that shares were bought.

`IN9` partly paid securities remain in the raw holdings and subtotal checks but are excluded from both rankings. The eight Bharti Airtel partly paid rows (`IN9397D01014`) therefore cannot appear as stock buys or sales. They are not added to ordinary Bharti Airtel (`INE397D01024`): the Focused scheme's ordinary share count also fell between March and April 2025, and [Airtel announced its first and final call only in December 2025](https://assets.airtel.in/static-assets/cms/investor/docs/First-and-Final-Call-22122025.pdf). The April 2025 change does not establish a conversion.

Original downloads stay in ignored `data/raw/`; normalized CSVs and manifests stay in ignored `data/generated/`. The pinned importers reject changed source checksums. **The return date range has not yet been extended with the holdings history.** It covers August 3–September 23, 2026: 37 trading days, 260 stock ISINs with complete series, and the official Nifty 500 total-return index. One held stock is excluded because its September 4 buyback needs a separate adjustment method. `compare` evaluates just **one** 30-day disclosure cohort (the August 8 decision); the September 8 cohort has no 30-day exit yet. Its paired difference is an illustration of the mechanics, not evidence of a predictive signal.

### Public price and return sources

The [NSE daily equity bhavcopy](https://www.nseindia.com/all-reports) includes ISIN-level closes. Archive a chosen period with:

```bash
python scripts/nse_raw_prices.py --start 2026-08-01 --end 2026-09-23
```

This writes `data/generated/nse_raw_closes.csv` and an audit manifest. **These closes are unadjusted and must not be imported directly.** The archive accepts NSE EQ and BE series, including a held security that moved between them during August. `build_return_prices.py` checks the pinned source hashes, compounds cash dividends on their ex-dates using the [NSE corporate-action feed](https://www.nseindia.com/companies-listing/corporate-filings-actions), excludes securities with other corporate actions, and imports a stock total-return index for each supported ISIN. It imports the official [Nifty 500 total-return index](https://www.niftyindices.com/reports/historical-data) as `NIFTY500_TRI`. This forward-indexed stock series preserves entry-to-exit total returns; its numeric level is not a rupee market price. The public sources can revise historical records, so a changed pinned response stops the build for review.

## How it works

1. Import a **complete** portfolio snapshot for each fund scheme and reporting period. Each snapshot has a period end, an availability cutoff with timezone, and a source URL. For the pilot, the cutoff follows the date-level rule above.
2. At an `--as-of` date, select only snapshots whose stored availability cutoff has passed by the end of that date in India. A later correction stays invisible to earlier decisions.
3. Compare the latest two visible snapshots within each scheme only when their reporting months are adjacent. For weight, a change of 0.01 percentage points or less is ignored. For quantity, the previous share count is first multiplied by any reviewed split/bonus factor; share changes of half a share or less are ignored.
4. Weight score: `up - down + clipped(net_weight_change_pp / 2, -1, 1)`. Quantity score: `up - down`, with a deterministic tie break. Both are hypotheses, not learned or validated models.
5. The original demo backtest uses month-end decisions. A disclosure-aware backtest uses `--schedule disclosure`, taking decisions at the end of each stored disclosure date. Either schedule enters at the first available adjusted close **after** the decision date; exits on the first available close at least `horizon_days` later; compares with the benchmark on the exact same dates; and subtracts a configurable round-trip cost.

An incomplete cohort is skipped. The reported average is across separate cohorts, **not** a compounded portfolio return; cohorts may overlap in time. `flowrank compare` evaluates weight and quantity on the same disclosure dates and reports only paired evaluated cohorts in its difference summary. With the current short price window, the August decision can be evaluated at 30 days and September is skipped. It cannot support a useful historical performance conclusion.

## Input contracts

`import-holdings` expects exactly these CSV columns:

| Column | Meaning |
| --- | --- |
| `scheme_id` | Stable scheme identifier; use the same ID across months |
| `asset_id` | Stable security identifier, preferably an ISIN for real equities |
| `period_end` | `YYYY-MM-DD` date the holdings describe |
| `published_at` | ISO 8601 availability cutoff, including timezone offset; use a verified publication timestamp when available |
| `weight_pct` | Portfolio weight in percentage units (for example, `2.5`) |
| `quantity` | Optional positive number of shares reported in the disclosure |
| `instrument_name` | Optional reported name; may change between months |
| `source_url` | URL of the original disclosure or archive |

Each `(scheme_id, period_end, published_at)` in an import file must represent the **whole relevant equity portfolio**, because an omitted security is interpreted as a zero weight. Use a new availability cutoff for a corrected filing; never overwrite historical knowledge.

`import-actions` expects `asset_id,effective_date,share_multiplier,description,source_url` for manually verified share-count adjustments. A 1:1 bonus, for example, has multiplier `2`. `import-action-reviews` expects `asset_id,start_date,end_date,source_url` documenting a complete review of quantity-changing actions over the inclusive interval. A review should not be entered merely because no events were noticed; identify and archive a source covering the interval. The pinned NSE review command generates these rows for the current pilot and stops if it finds an unclassified or share-changing event.

`import-prices` expects `asset_id,price_date,adjusted_close,source_url`. Prices must be adjusted consistently for splits, dividends and other corporate actions, including the benchmark series. Do not pass `nse_raw_closes.csv` directly to this importer; use the checked return builder for the pinned short pilot. Use historical constituents when expanding the investable universe. Never treat a current stock list as the past universe.

Example commands after preparing your own CSVs:

```bash
flowrank init --db data/research.sqlite
flowrank import-holdings --db data/research.sqlite --file path/to/holdings.csv
flowrank import-prices --db data/research.sqlite --file path/to/prices.csv
flowrank rank --db data/research.sqlite --as-of 2025-04-30
flowrank backtest --db data/research.sqlite --start 2020-01-01 --end 2025-12-31 --schedule disclosure --signal weight --horizon-days 90 --top 20 --benchmark YOUR_TOTAL_RETURN_BENCHMARK_ID --cost-bps 25
flowrank compare --db data/research.sqlite --start 2020-01-01 --end 2025-12-31 --horizon-days 90 --top 20 --benchmark YOUR_TOTAL_RETURN_BENCHMARK_ID --cost-bps 25
```

## What to build next

1. **Broaden disclosures:** extend across more schemes and years, including archived revisions. Verify first-publication timing where a timestamp is available. [AMFI has a portfolio disclosure portal](https://www.amfiindia.com/online-center/portfolio-disclosure); check coverage and download terms before scaling.
2. **Longer return and action history:** archive NSE closes and the Nifty 500 total-return index from early 2025 onward without overwriting earlier batches. The current raw-price command caps one run at 367 calendar days, while the return builder and action review use pinned short-window catalogs. Extend those catalogs and handle splits, bonuses, buybacks, identifier changes, delistings, and missing closes before claiming additional evaluated cohorts. Keep original source data and adjustment provenance.
3. **Research controls:** build historical Nifty 500 membership, delisted-stock coverage, liquidity filters, market-cap/sector baselines, and multiple horizons. Track missing data instead of silently dropping it. Evaluate on later untouched years after choosing rules on earlier years. Check scheme-level investor flows before interpreting share-count increases as discretionary buying.
4. **Additional signals:** add published [NSE shareholding filings](https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern), financial statements, insider filings and adjusted market data. Keep every feature tied to when it became public.
5. **Model only if warranted:** compare any ML ranker with this frozen rules baseline on truly out-of-sample data. A more complex model is useful only if it improves the result after costs and realistic coverage rules.

### Current limits

This is a research scaffold, not an investment recommendation or live trading system. The real-data pilot covers 20 disclosure months, five schemes from one fund house, and only 37 trading days of returns. Most historical disclosure cutoffs are proxies because their notice dates are unverified. It has a narrow corporate-action review for July–August 2026 and a short dividend-aware return series, but no full adjustment history, investor-flow adjustment, survivorship-safe historical universe, tax model, or executable portfolio simulation. Share-count increases do not by themselves establish manager intent. One completed cohort cannot validate the investment hypothesis. A larger point-in-time archive and a preregistered historical test are still needed.
