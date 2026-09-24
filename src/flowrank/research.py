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


@dataclass(frozen=True)
class QuantityRank:
    asset_id: str
    increasing_schemes: int
    decreasing_schemes: int
    new_positions: int
    net_share_change: float
    score: int
    unreviewed_pairs: int


def _visible_snapshots(db: sqlite3.Connection, as_of: date) -> dict[str, list[sqlite3.Row]]:
    cutoff = datetime.combine(as_of, time.max, IST).astimezone(timezone.utc).isoformat(timespec="seconds")
    snapshots = db.execute(
        """SELECT id, scheme_id, period_end, published_at
           FROM holding_snapshots
           WHERE published_at <= ? AND period_end <= ?
           ORDER BY scheme_id, period_end DESC, published_at DESC""",
        (cutoff, as_of.isoformat()),
    ).fetchall()
    selected: dict[str, list[sqlite3.Row]] = defaultdict(list)
    periods: dict[str, set[str]] = defaultdict(set)
    for row in snapshots:
        scheme = row["scheme_id"]
        if row["period_end"] not in periods[scheme] and len(selected[scheme]) < 2:
            selected[scheme].append(row)
            periods[scheme].add(row["period_end"])
    # Sparse archives must not turn a gap of several months into a claimed
    # month-on-month signal. Keep only adjacent reporting months.
    return {scheme: pair for scheme, pair in selected.items()
            if len(pair) == 2 and _adjacent_months(pair[1]["period_end"], pair[0]["period_end"])}


def _adjacent_months(previous: str, current: str) -> bool:
    older, newer = date.fromisoformat(previous), date.fromisoformat(current)
    following = date(older.year + (older.month == 12), older.month % 12 + 1, 1)
    return (newer.year, newer.month) == (following.year, following.month)


def rank(db: sqlite3.Connection, as_of: date) -> list[Rank]:
    """Compare each scheme's two latest visible complete portfolios by weight."""
    selected = _visible_snapshots(db, as_of)

    holdings: dict[int, dict[str, float]] = {}
    for snapshots in selected.values():
        for snapshot in snapshots:
            holdings[snapshot["id"]] = {
                row["asset_id"]: row["weight_pct"]
                for row in db.execute(
                    "SELECT asset_id, weight_pct FROM holdings WHERE snapshot_id=?", (snapshot["id"],)
                )
            }

    changes = defaultdict(lambda: {"up": 0, "down": 0, "new": 0, "delta": 0.0})
    for snapshots in selected.values():
        if len(snapshots) < 2:
            continue
        current, previous = holdings[snapshots[0]["id"]], holdings[snapshots[1]["id"]]
        for asset in current.keys() | previous.keys():
            # IN9 identifies temporary/partly paid securities in this pilot.
            # Their disappearance must not be reported as an ordinary-stock sale.
            if asset.startswith("IN9"):
                continue
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


def _action_reviewed(db: sqlite3.Connection, asset: str, previous: date, current: date) -> bool:
    """Every day after the older snapshot must be covered by a source review."""
    covered_through = previous
    for row in db.execute(
        "SELECT start_date, end_date FROM action_reviews WHERE asset_id=? AND end_date>? AND start_date<=? "
        "ORDER BY start_date, end_date", (asset, previous.isoformat(), current.isoformat())
    ):
        start, end = date.fromisoformat(row["start_date"]), date.fromisoformat(row["end_date"])
        if start > covered_through + timedelta(days=1):
            return False
        covered_through = max(covered_through, end)
        if covered_through >= current:
            return True
    return False


def _share_multiplier(db: sqlite3.Connection, asset: str, previous: date, current: date) -> float:
    factor = 1.0
    for row in db.execute(
        "SELECT share_multiplier FROM share_actions WHERE asset_id=? AND effective_date>? "
        "AND effective_date<=? ORDER BY effective_date", (asset, previous.isoformat(), current.isoformat())
    ):
        factor *= row["share_multiplier"]
    return factor


def quantity_rank(db: sqlite3.Connection, as_of: date, allow_unreviewed_actions: bool = False) -> list[QuantityRank]:
    """Count net share increases after declared share actions; require action reviews by default."""
    selected = _visible_snapshots(db, as_of)
    holdings: dict[int, dict[str, float | None]] = {}
    for snapshots in selected.values():
        for snapshot in snapshots:
            holdings[snapshot["id"]] = {
                row["asset_id"]: row["quantity"]
                for row in db.execute("SELECT asset_id, quantity FROM holdings WHERE snapshot_id=?",
                                      (snapshot["id"],))
            }
    changes = defaultdict(lambda: {"up": 0, "down": 0, "new": 0, "delta": 0.0, "unreviewed": 0})
    incomplete_assets = set()
    for snapshots in selected.values():
        if len(snapshots) < 2:
            continue
        current, previous = holdings[snapshots[0]["id"]], holdings[snapshots[1]["id"]]
        current_date = date.fromisoformat(snapshots[0]["period_end"])
        previous_date = date.fromisoformat(snapshots[1]["period_end"])
        for asset in current.keys() | previous.keys():
            if asset.startswith("IN9"):
                continue
            if (asset in current and current[asset] is None or
                    asset in previous and previous[asset] is None):
                incomplete_assets.add(asset)
                continue
            reviewed = _action_reviewed(db, asset, previous_date, current_date)
            if not reviewed and not allow_unreviewed_actions:
                incomplete_assets.add(asset)
                continue
            prior_quantity = (previous.get(asset) or 0.0) * _share_multiplier(
                db, asset, previous_date, current_date)
            delta = (current.get(asset) or 0.0) - prior_quantity
            if abs(delta) <= 0.5:  # whole shares; suppress floating-point noise
                continue
            item = changes[asset]
            item["delta"] += delta
            item["unreviewed"] += int(not reviewed)
            if delta > 0:
                item["up"] += 1
                if asset not in previous:
                    item["new"] += 1
            else:
                item["down"] += 1
    result = [QuantityRank(asset, item["up"], item["down"], item["new"], item["delta"],
                           item["up"] - item["down"], item["unreviewed"])
              for asset, item in changes.items() if asset not in incomplete_assets]
    return sorted(result, key=lambda row: (-row.score, -row.increasing_schemes, row.asset_id))


def month_ends(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        last = date(year, month, calendar.monthrange(year, month)[1])
        if start <= last <= end:
            yield last
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def disclosure_dates(db: sqlite3.Connection, start: date, end: date) -> list[date]:
    """Use the stored disclosure cutoffs, expressed in India time, as decisions."""
    dates = {datetime.fromisoformat(row[0]).astimezone(IST).date()
             for row in db.execute("SELECT DISTINCT published_at FROM holding_snapshots")}
    return sorted(day for day in dates if start <= day <= end)


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
    signal: str = "weight",
    schedule: str = "month_end",
    allow_unreviewed_actions: bool = False,
) -> list[dict]:
    """Separate cohorts, entered after the decision date; no portfolio compounding."""
    if (start > end or horizon_days < 1 or top < 1 or cost_bps < 0
            or signal not in {"weight", "quantity"} or schedule not in {"month_end", "disclosure"}):
        raise ValueError("Invalid backtest dates, horizon, top count, cost, signal or schedule")
    results = []
    decisions = month_ends(start, end) if schedule == "month_end" else disclosure_dates(db, start, end)
    for decision in decisions:
        candidates = (rank(db, decision) if signal == "weight" else
                      quantity_rank(db, decision, allow_unreviewed_actions))[:top]
        record = {"decision_date": decision.isoformat(), "signal": signal, "schedule": schedule,
                  "status": "skipped", "reason": ""}
        if len(candidates) < top:
            if signal == "quantity" and not allow_unreviewed_actions and not db.execute(
                "SELECT 1 FROM action_reviews LIMIT 1").fetchone():
                record["reason"] = "no corporate-action review coverage imported"
            else:
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
