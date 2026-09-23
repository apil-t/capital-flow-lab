"""Explainable fund-allocation ranking and a simple cohort backtest."""

import calendar
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Rank:
    asset_id: str
    increasing_schemes: int
    decreasing_schemes: int
    new_positions: int
    net_weight_change_pp: float
    score: float


def rank(db: sqlite3.Connection, as_of: date) -> list[Rank]:
    """Compare each scheme's two latest *published* complete portfolios."""
    cutoff = datetime.combine(as_of, time.max, IST).astimezone(timezone.utc).isoformat(timespec="seconds")
    snapshots = db.execute(
        """SELECT id, scheme_id, period_end, published_at
           FROM holding_snapshots
           WHERE published_at <= ? AND period_end <= ?
           ORDER BY scheme_id, period_end DESC, published_at DESC""",
        (cutoff, as_of.isoformat()),
    ).fetchall()
    selected: dict[str, list[int]] = defaultdict(list)
    periods: dict[str, set[str]] = defaultdict(set)
    for row in snapshots:
        scheme = row["scheme_id"]
        if row["period_end"] not in periods[scheme] and len(selected[scheme]) < 2:
            selected[scheme].append(row["id"])
            periods[scheme].add(row["period_end"])

    holdings: dict[int, dict[str, float]] = {}
    for ids in selected.values():
        for snapshot_id in ids:
            holdings[snapshot_id] = {
                row["asset_id"]: row["weight_pct"]
                for row in db.execute(
                    "SELECT asset_id, weight_pct FROM holdings WHERE snapshot_id=?", (snapshot_id,)
                )
            }

    changes = defaultdict(lambda: {"up": 0, "down": 0, "new": 0, "delta": 0.0})
    for ids in selected.values():
        if len(ids) < 2:
            continue
        current, previous = holdings[ids[0]], holdings[ids[1]]
        for asset in current.keys() | previous.keys():
            delta = current.get(asset, 0.0) - previous.get(asset, 0.0)
            if abs(delta) <= 0.01:  # ignore rounding noise in published weights
                continue
            item = changes[asset]
            item["delta"] += delta
            if delta > 0:
                item["up"] += 1
                if asset not in previous:
                    item["new"] += 1
            else:
                item["down"] += 1

    result = []
    for asset, item in changes.items():
        # Breadth is primary; clipped weight change breaks ties without letting one
        # large scheme dominate. These are research heuristics, not learned weights.
        score = item["up"] - item["down"] + max(-1.0, min(1.0, item["delta"] / 2.0))
        result.append(Rank(asset, item["up"], item["down"], item["new"], item["delta"], score))
    return sorted(result, key=lambda row: (-row.score, row.asset_id))


def month_ends(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        last = date(year, month, calendar.monthrange(year, month)[1])
        if start <= last <= end:
            yield last
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _price(db: sqlite3.Connection, asset: str, earliest: date, max_lag_days: int):
    row = db.execute(
        """SELECT price_date, adjusted_close FROM prices
           WHERE asset_id=? AND price_date>=?
           ORDER BY price_date LIMIT 1""",
        (asset, earliest.isoformat()),
    ).fetchone()
    if row and date.fromisoformat(row["price_date"]) <= earliest + timedelta(days=max_lag_days):
        return date.fromisoformat(row["price_date"]), row["adjusted_close"]
    return None


def _exact_price(db: sqlite3.Connection, asset: str, day: date):
    row = db.execute(
        "SELECT adjusted_close FROM prices WHERE asset_id=? AND price_date=?",
        (asset, day.isoformat()),
    ).fetchone()
    return row["adjusted_close"] if row else None


def backtest(
    db: sqlite3.Connection,
    start: date,
    end: date,
    horizon_days: int,
    top: int,
    benchmark: str,
    cost_bps: float,
) -> list[dict]:
    """Independent monthly cohorts, entered after decision date; no portfolio compounding."""
    if start > end or horizon_days < 1 or top < 1 or cost_bps < 0:
        raise ValueError("Invalid backtest dates, horizon, top count or cost")
    results = []
    for decision in month_ends(start, end):
        candidates = rank(db, decision)[:top]
        record = {"decision_date": decision.isoformat(), "status": "skipped", "reason": ""}
        if len(candidates) < top:
            record["reason"] = f"only {len(candidates)} ranked assets"
            results.append(record)
            continue
        gross_returns = []
        excess_returns = []
        for candidate in candidates:
            entry = _price(db, candidate.asset_id, decision + timedelta(days=1), 5)
            if entry is None:
                record["reason"] = f"missing timely entry price: {candidate.asset_id}"
                break
            exit_price = _price(db, candidate.asset_id, entry[0] + timedelta(days=horizon_days), 5)
            if exit_price is None:
                record["reason"] = f"missing timely exit price: {candidate.asset_id}"
                break
            benchmark_entry = _exact_price(db, benchmark, entry[0])
            benchmark_exit = _exact_price(db, benchmark, exit_price[0])
            if benchmark_entry is None or benchmark_exit is None:
                record["reason"] = f"missing benchmark prices: {entry[0]} or {exit_price[0]}"
                break
            stock_return = exit_price[1] / entry[1] - 1
            benchmark_return = benchmark_exit / benchmark_entry - 1
            gross_returns.append(stock_return)
            excess_returns.append(stock_return - benchmark_return - cost_bps / 10_000)
        else:
            record.update(
                status="evaluated",
                assets=[candidate.asset_id for candidate in candidates],
                mean_gross_return=sum(gross_returns) / top,
                mean_net_excess_return=sum(excess_returns) / top,
            )
        results.append(record)
    return results
