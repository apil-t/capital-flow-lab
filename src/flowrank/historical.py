"""Pinned, retrospective NSE return and corporate-action coverage."""

import csv
import hashlib
import io
import json
import math
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .nse_actions import _fetch_pinned
from .research import _adjacent_months
from .returns import DIVIDEND_AMOUNT, _raw_closes, _tri_rows
from .storage import import_action_reviews, import_prices, import_share_actions


BONUS = re.compile(r"^Bonus\s+(\d+)\s*:\s*(\d+)$", re.IGNORECASE)
BENIGN = {"annual general meeting"}


def _sources(catalog_path: Path, raw_dir: Path, raw_csv: Path, raw_manifest: Path):
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    start, end = date.fromisoformat(catalog["start_date"]), date.fromisoformat(catalog["end_date"])
    closes = _raw_closes(raw_csv, raw_manifest, catalog)
    path = _fetch_pinned(catalog["action_source"], raw_dir, True)
    actions = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(actions, list) or not actions:
        raise ValueError("Empty NSE corporate-action archive")
    for action in actions:
        day = datetime.strptime(action["exDate"], "%d-%b-%Y").date()
        if not start <= day <= end:
            raise ValueError(f"Corporate action outside pinned range: {action}")
    # The action feed often retains an older ISIN after NSE bhavcopies have
    # switched to the current one. Verify ticker and issuer prefix using the
    # same pinned daily archives, rather than silently dropping these events.
    symbols = defaultdict(set)
    manifest = json.loads(raw_manifest.read_text(encoding="utf-8"))
    for day in manifest["days"]:
        with zipfile.ZipFile(day["local_path"]) as archive:
            members = [name for name in archive.namelist() if name.endswith(".csv")]
            if len(members) != 1:
                raise ValueError(f"Unexpected bhavcopy layout: {day['date']}")
            with archive.open(members[0]) as source:
                reader = csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig"))
                if not {"ISIN", "TckrSymb", "SctySrs"} <= set(reader.fieldnames or []):
                    raise ValueError(f"Bhavcopy lacks ticker symbols: {day['date']}")
                for row in reader:
                    if row["ISIN"] in closes and row["SctySrs"] in {"EQ", "BE"}:
                        symbols[row["ISIN"]].add(row["TckrSymb"].upper())
    return catalog, closes, actions, symbols


def _events(actions: list[dict], closes: dict, symbols: dict) -> tuple[dict, dict, dict, list, list]:
    """Recognize cash dividends and same-ISIN bonuses; quarantine other events."""
    dividends = defaultdict(float)
    bonuses = {}
    blocked = defaultdict(list)
    spans = {asset: (min(row[0] for row in values), max(row[0] for row in values))
             for asset, values in closes.items()}
    dates = {asset: {row[0] for row in values} for asset, values in closes.items()}
    unmatched = []
    aliases = []
    for action in actions:
        old_isin = action.get("isin", "")
        day = datetime.strptime(action["exDate"], "%d-%b-%Y").date()
        subject = action["subject"].strip()
        lower = subject.lower()
        ticker = action.get("symbol", "").upper()
        candidates = [asset for asset in closes if asset[:7] == old_isin[:7]
                      and (ticker in symbols.get(asset, ()) or not ticker)]
        if not candidates:
            if any(asset[:7] == old_isin[:7] for asset in closes):
                unmatched.append((old_isin, day.isoformat(), ticker, subject))
            continue
        on_ex_date = [asset for asset in candidates if day in dates[asset]]
        around_ex_date = [asset for asset in candidates
                          if spans[asset][0] <= day <= spans[asset][1]
                          or 0 <= (day - spans[asset][1]).days <= 5
                          or 0 <= (spans[asset][0] - day).days <= 5]
        resolved = sorted(set(on_ex_date) | set(around_ex_date))
        if resolved and (old_isin not in resolved or len(resolved) > 1):
            aliases.append({"action_isin": old_isin, "traded_isins": resolved,
                            "ex_date": day.isoformat(), "symbol": ticker,
                            "subject": subject})
        if lower in BENIGN:
            continue
        amount = DIVIDEND_AMOUNT.findall(subject) if "dividend" in lower else []
        if "dividend" in lower and len(amount) >= 1 and len(amount) == lower.count("dividend") and not any(
            word in lower for word in ("bonus", "split", "rights", "demerger", "buyback", "buy back")):
            value = sum(float(part) for part in amount)
            if math.isfinite(value) and value > 0 and len(on_ex_date) == 1:
                dividends[(on_ex_date[0], day)] += value
                continue
        match = BONUS.fullmatch(subject)
        if match:
            numerator, denominator = map(int, match.groups())
            if denominator and len(on_ex_date) == 1 and spans[on_ex_date[0]][0] < day:
                key = (on_ex_date[0], day)
                if key in bonuses:
                    raise ValueError(f"Duplicate bonus for {key[0]} on {day}")
                bonuses[key] = 1 + numerator / denominator
                continue
        for asset in around_ex_date:
            blocked[asset].append((day, subject))
    return dividends, bonuses, blocked, unmatched, aliases


def build_historical_prices(db, catalog_path: Path, raw_dir: Path, output_dir: Path,
                            raw_csv: Path, raw_manifest: Path) -> dict:
    catalog, closes, actions, symbols = _sources(catalog_path, raw_dir, raw_csv, raw_manifest)
    start, end = date.fromisoformat(catalog["start_date"]), date.fromisoformat(catalog["end_date"])
    tri = _tri_rows(catalog["tri_source"], raw_dir, start, end)
    trading_days = {day for day, _ in tri}
    if trading_days != {day for values in closes.values() for day, _, _ in values}:
        raise ValueError("Nifty 500 TRI trading days differ from NSE bhavcopy days")
    dividends, bonuses, blocked, unmatched, aliases = _events(actions, closes, symbols)
    if unmatched:
        raise ValueError(f"NSE action ISIN/ticker aliases need review: {unmatched[:5]}")
    output_rows = []
    excluded = {}
    coverage = {}
    for asset, values in sorted(closes.items()):
        values.sort()
        first, last = values[0][0], values[-1][0]
        reasons = [f"{day}: {subject}" for day, subject in blocked.get(asset, [])
                   if first < day <= last]
        available = {day for day, _, _ in values}
        missing_ex_dates = [day for (isin, day) in set(dividends) | set(bonuses)
                            if isin == asset and first < day <= last and day not in available]
        reasons += [f"corporate-action ex-date lacks a close: {day}" for day in missing_ex_dates]
        if reasons:
            excluded[asset] = reasons
            continue
        adjusted = prior_close = values[0][1]
        for index, (day, close, source_url) in enumerate(values):
            if index:
                factor = bonuses.get((asset, day), 1.0)
                adjusted *= (close * factor + dividends.get((asset, day), 0.0)) / prior_close
                prior_close = close
            output_rows.append((asset, day.isoformat(), f"{adjusted:.12g}", source_url))
        coverage[asset] = {"first": first.isoformat(), "last": last.isoformat(), "days": len(values)}
    for day, value in tri:
        output_rows.append((catalog["benchmark_id"], day.isoformat(), f"{value:.12g}",
                            catalog["tri_source"]["url"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_csv = output_dir / "historical_total_return_prices.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("asset_id", "price_date", "adjusted_close", "source_url"))
        writer.writerows(sorted(output_rows))
    count = import_prices(db, output_csv)
    manifest = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "trading_days": len(trading_days), "price_rows": count,
                "stock_isins": len(coverage), "excluded_isins": excluded,
                "cash_dividend_events": len(dividends), "bonus_events": len(bonuses),
                "unmatched_action_aliases": unmatched,
                "resolved_action_aliases": aliases,
                "coverage": coverage,
                "source_catalog": str(catalog_path),
                "price_csv_sha256": hashlib.sha256(output_csv.read_bytes()).hexdigest(),
                "method": "NSE EQ/BE closes compounded with cash dividends and verified same-ISIN bonus factors; exclude securities whose observed span crosses an unsupported event"}
    (output_dir / "historical_total_return_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def build_historical_action_reviews(db, catalog_path: Path, raw_dir: Path, output_dir: Path,
                                    raw_csv: Path, raw_manifest: Path) -> dict:
    catalog, closes, actions, symbols = _sources(catalog_path, raw_dir, raw_csv, raw_manifest)
    _, bonuses, blocked, unmatched, aliases = _events(actions, closes, symbols)
    if unmatched:
        raise ValueError(f"NSE action ISIN/ticker aliases need review: {unmatched[:5]}")
    for change in catalog.get("isin_changes", []):
        old, new, day = change["old"], change["new"], date.fromisoformat(change["ex_date"])
        if not any(event_day == day for event_day, _ in blocked.get(old, [])):
            raise ValueError(f"ISIN change lacks a pinned NSE action: {old} {day}")
        blocked[new].append((day, f"identifier change from {old}"))
    available_months = {asset: {(day.year, day.month) for day, _, _ in values}
                        for asset, values in closes.items()}
    rows = db.execute(
        "SELECT s.period_end, h.asset_id FROM holding_snapshots s "
        "JOIN holdings h ON h.snapshot_id=s.id WHERE h.quantity IS NOT NULL "
        "AND h.asset_id NOT LIKE 'IN9%'"
    )
    holdings = defaultdict(set)
    for row in rows:
        holdings[row["period_end"]].add(row["asset_id"])
    if not holdings:
        raise ValueError("Import HDFC holdings before building historical action reviews")
    periods = sorted(date.fromisoformat(day) for day in holdings)
    review_end = date.fromisoformat(catalog["holdings_review_end_date"])
    source_url = catalog["action_source"]["url"]
    review_rows = []
    skipped = defaultdict(int)
    for previous, current in zip(periods, periods[1:]):
        if current > review_end or not _adjacent_months(previous.isoformat(), current.isoformat()):
            continue
        for asset in sorted(holdings[previous.isoformat()] | holdings[current.isoformat()]):
            if any(previous < day <= current for day, _ in blocked.get(asset, [])):
                skipped["noncash_event_or_identifier_change"] += 1
                continue
            months = available_months.get(asset, set())
            if (previous.year, previous.month) not in months or (current.year, current.month) not in months:
                skipped["no_NSE_close_in_both_months"] += 1
                continue
            review_rows.append((asset, (previous + timedelta(days=1)).isoformat(),
                                current.isoformat(), source_url))
    output_dir.mkdir(parents=True, exist_ok=True)
    review_csv = output_dir / "historical_action_reviews.csv"
    with review_csv.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("asset_id", "start_date", "end_date", "source_url"))
        writer.writerows(review_rows)
    reviewed = import_action_reviews(db, review_csv) if review_rows else 0
    bonus_csv = output_dir / "historical_share_actions.csv"
    with bonus_csv.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("asset_id", "effective_date", "share_multiplier", "description", "source_url"))
        writer.writerows((asset, day.isoformat(), factor, "NSE bonus shares", source_url)
                         for (asset, day), factor in sorted(bonuses.items()))
    if bonuses:
        import_share_actions(db, bonus_csv)
    manifest = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "periods": len(periods), "reviewed_asset_months": reviewed,
                "bonus_adjustments": len(bonuses), "skipped": dict(skipped),
                "unmatched_action_aliases": unmatched,
                "resolved_action_aliases": aliases,
                "source_catalog": str(catalog_path),
                "scope": "Only HDFC asset-month pairs trading on NSE in both reporting months; noncash and identifier-change intervals are withheld"}
    (output_dir / "historical_action_review_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
