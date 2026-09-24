"""Predefined cross-sectional diagnostics for the HDFC holdings pilot."""

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from .research import (_action_reviewed, _exact_price, _price, _visible_snapshots,
                       backtest, disclosure_dates, quantity_rank, rank)


def _midranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        stop = start + 1
        while stop < len(order) and values[order[stop]] == values[order[start]]:
            stop += 1
        middle = (start + 1 + stop) / 2
        for index in order[start:stop]:
            result[index] = middle
        start = stop
    return result


def spearman(scores: list[float], outcomes: list[float]) -> float | None:
    """Pearson correlation of midranks, retaining entire tied-score groups."""
    if len(scores) < 2 or len(scores) != len(outcomes):
        return None
    x, y = _midranks(scores), _midranks(outcomes)
    xmean, ymean = statistics.mean(x), statistics.mean(y)
    numerator = sum((a - xmean) * (b - ymean) for a, b in zip(x, y))
    xvariance = sum((a - xmean) ** 2 for a in x)
    yvariance = sum((b - ymean) ** 2 for b in y)
    if xvariance == 0 or yvariance == 0:
        return None
    return numerator / math.sqrt(xvariance * yvariance)


def _full_scores(db, decision: date) -> tuple[dict[str, float], dict[str, float | None]]:
    selected = _visible_snapshots(db, decision)
    universe = set()
    unavailable = set()
    for pair in selected.values():
        current = {row["asset_id"]: row["quantity"] for row in db.execute(
            "SELECT asset_id, quantity FROM holdings WHERE snapshot_id=?", (pair[0]["id"],))}
        previous = {row["asset_id"]: row["quantity"] for row in db.execute(
            "SELECT asset_id, quantity FROM holdings WHERE snapshot_id=?", (pair[1]["id"],))}
        old_day, new_day = date.fromisoformat(pair[1]["period_end"]), date.fromisoformat(pair[0]["period_end"])
        for asset in current.keys() | previous.keys():
            if asset.startswith("IN9"):
                continue
            universe.add(asset)
            if ((asset in current and current[asset] is None) or
                    (asset in previous and previous[asset] is None) or
                    not _action_reviewed(db, asset, old_day, new_day)):
                unavailable.add(asset)
    weights = {row.asset_id: row.score for row in rank(db, decision)}
    quantities = {row.asset_id: row.score for row in quantity_rank(db, decision)}
    return ({asset: weights.get(asset, 0.0) for asset in universe},
            {asset: None if asset in unavailable else quantities.get(asset, 0.0)
             for asset in universe})


def _outcome(db, asset: str, decision: date, protocol: dict) -> tuple[float | None, str | None]:
    entry = _price(db, asset, decision + timedelta(days=1),
                   protocol["max_entry_lag_calendar_days"])
    if entry is None:
        return None, "missing_timely_entry"
    exit_price = _price(db, asset,
                        entry[0] + timedelta(days=protocol["horizon_calendar_days_from_entry"]),
                        protocol["max_exit_lag_calendar_days"])
    if exit_price is None:
        return None, "missing_timely_exit"
    benchmark_entry = _exact_price(db, protocol["benchmark_id"], entry[0])
    benchmark_exit = _exact_price(db, protocol["benchmark_id"], exit_price[0])
    if benchmark_entry is None or benchmark_exit is None:
        return None, "missing_benchmark"
    return (exit_price[1] / entry[1] - 1 -
            (benchmark_exit / benchmark_entry - 1)), None


def _signal_result(scores: dict[str, float | None], outcomes: dict[str, float]):
    eligible = {asset: score for asset, score in scores.items()
                if score is not None and asset in outcomes}
    groups = defaultdict(list)
    for asset, score in eligible.items():
        group = "negative" if score < 0 else "positive" if score > 0 else "zero"
        groups[group].append(outcomes[asset])
    by_group = {group: {"stocks": len(groups[group]),
                        "mean_gross_excess_return": statistics.mean(groups[group]) if groups[group] else None}
                for group in ("negative", "zero", "positive")}
    ic = spearman([eligible[a] for a in sorted(eligible)],
                  [outcomes[a] for a in sorted(eligible)])
    return {"priced_stocks": len(eligible), "rank_ic": ic, "groups": by_group,
            "positive_minus_negative": (
                by_group["positive"]["mean_gross_excess_return"] -
                by_group["negative"]["mean_gross_excess_return"]
                if groups["positive"] and groups["negative"] else None)}


def evaluate_one_month(db, protocol_path: Path, output_path: Path) -> dict:
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    start, end = date.fromisoformat(protocol["decision_start"]), date.fromisoformat(protocol["decision_end"])
    decisions = disclosure_dates(db, start, end)
    top_results = {signal: {row["decision_date"]: row for row in backtest(
        db, start, end, protocol["horizon_calendar_days_from_entry"], 2,
        protocol["benchmark_id"], 25, signal=signal, schedule="disclosure")}
                   for signal in ("weight", "quantity")}
    monthly = []
    for decision in decisions:
        weight, quantity = _full_scores(db, decision)
        if not weight:
            continue
        priced = {}
        missing = Counter()
        for asset in weight:
            excess, reason = _outcome(db, asset, decision, protocol)
            if reason:
                missing[reason] += 1
            else:
                priced[asset] = excess
        valid_weight = {asset: score for asset, score in weight.items()}
        valid_quantity = {asset: score for asset, score in quantity.items()}
        common = sorted(asset for asset in priced if quantity[asset] is not None)
        result = {"decision_date": decision.isoformat(), "universe_stocks": len(weight),
                  "stocks_with_return": len(priced), "missing_return": dict(missing),
                  "quantity_action_reviewed_stocks": sum(score is not None for score in quantity.values()),
                  "weight": _signal_result(valid_weight, priced),
                  "quantity": _signal_result(valid_quantity, priced),
                  "paired_common_stocks": len(common),
                  "paired_rank_ic": {
                      "weight": spearman([weight[a] for a in common], [priced[a] for a in common]),
                      "quantity": spearman([quantity[a] for a in common], [priced[a] for a in common])},
                  "top_two": {signal: top_results[signal].get(decision.isoformat())
                              for signal in ("weight", "quantity")}}
        monthly.append(result)
    summary = {}
    for signal in ("weight", "quantity"):
        correlations = [row[signal]["rank_ic"] for row in monthly if row[signal]["rank_ic"] is not None]
        top = [row["top_two"][signal]["mean_net_excess_return"] for row in monthly
               if row["top_two"][signal] and row["top_two"][signal]["status"] == "evaluated"]
        spreads = [row[signal]["positive_minus_negative"] for row in monthly
                   if row[signal]["positive_minus_negative"] is not None]
        group_summary = {}
        for group in ("negative", "zero", "positive"):
            values = [row[signal]["groups"][group] for row in monthly
                      if row[signal]["groups"][group]["stocks"]]
            group_summary[group] = {
                "dates": len(values),
                "mean_stocks_per_date": statistics.mean(item["stocks"] for item in values) if values else None,
                "mean_date_equal_gross_excess_return": statistics.mean(
                    item["mean_gross_excess_return"] for item in values) if values else None}
        summary[signal] = {"rank_ic_dates": len(correlations),
                           "mean_rank_ic": statistics.mean(correlations) if correlations else None,
                           "median_rank_ic": statistics.median(correlations) if correlations else None,
                           "group_spread_dates": len(spreads),
                           "mean_positive_minus_negative": statistics.mean(spreads) if spreads else None,
                           "groups": group_summary,
                           "top_two_evaluated_dates": len(top),
                           "top_two_skipped_dates": len(monthly) - len(top),
                           "mean_top_two_net_excess_return": statistics.mean(top) if top else None}
    paired = [row["paired_rank_ic"] for row in monthly
              if all(value is not None for value in row["paired_rank_ic"].values())]
    summary["paired"] = {"dates": len(paired),
                         "mean_weight_rank_ic": statistics.mean(row["weight"] for row in paired) if paired else None,
                         "mean_quantity_rank_ic": statistics.mean(row["quantity"] for row in paired) if paired else None}
    paired_top = [row["top_two"] for row in monthly
                  if all(row["top_two"][signal] and row["top_two"][signal]["status"] == "evaluated"
                         for signal in ("weight", "quantity"))]
    summary["paired_top_two"] = {
        "dates": len(paired_top),
        "mean_weight_net_excess_return": statistics.mean(
            row["weight"]["mean_net_excess_return"] for row in paired_top) if paired_top else None,
        "mean_quantity_net_excess_return": statistics.mean(
            row["quantity"]["mean_net_excess_return"] for row in paired_top) if paired_top else None}
    result = {"protocol": str(protocol_path),
              "protocol_sha256": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
              "summary": summary, "monthly": monthly}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    def shown(value, percent=False):
        return "—" if value is None else f"{value * 100:+.2f}%" if percent else f"{value:+.3f}"

    def shown_count(value):
        return "—" if value is None else f"{value:.0f}"

    lines = ["# HDFC one-month exploratory evaluation", "",
             f"Protocol SHA-256: `{result['protocol_sha256']}`", "",
             "Date-wise rank IC uses midranks for ties. Return groups are negative, zero and positive",
             "signal scores; their means are gross benchmark-excess returns, averaged equally across dates.",
             "Top-two returns are equal-weight benchmark excess after a 25 bps round-trip cost.", "",
             "| Signal | Mean IC (dates) | Negative (avg stocks) | Zero (avg stocks) | Positive (avg stocks) | Positive − negative | Top-two net excess (dates) |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for signal in ("weight", "quantity"):
        item = summary[signal]
        groups = item["groups"]
        lines.append(f"| {signal} | {shown(item['mean_rank_ic'])} ({item['rank_ic_dates']}) | "
                     f"{shown(groups['negative']['mean_date_equal_gross_excess_return'], True)} "
                     f"({shown_count(groups['negative']['mean_stocks_per_date'])}) | "
                     f"{shown(groups['zero']['mean_date_equal_gross_excess_return'], True)} "
                     f"({shown_count(groups['zero']['mean_stocks_per_date'])}) | "
                     f"{shown(groups['positive']['mean_date_equal_gross_excess_return'], True)} "
                     f"({shown_count(groups['positive']['mean_stocks_per_date'])}) | "
                     f"{shown(item['mean_positive_minus_negative'], True)} | "
                     f"{shown(item['mean_top_two_net_excess_return'], True)} "
                     f"({item['top_two_evaluated_dates']}) |")
    lines += ["", f"On the same priced and action-reviewed stocks, mean IC was "
              f"{shown(summary['paired']['mean_weight_rank_ic'])} for weight and "
              f"{shown(summary['paired']['mean_quantity_rank_ic'])} for quantity "
              f"across {summary['paired']['dates']} dates. On the "
              f"{summary['paired_top_two']['dates']} dates with both top-two portfolios evaluated, "
              f"net excess return averaged "
              f"{shown(summary['paired_top_two']['mean_weight_net_excess_return'], True)} for weight "
              f"and {shown(summary['paired_top_two']['mean_quantity_net_excess_return'], True)} "
              f"for quantity.", "",
              "## Per decision date", "",
              "| Decision | Priced / universe | Quantity reviewed | Weight IC | Quantity IC | Weight top-two | Quantity top-two |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in monthly:
        top_weight = row["top_two"]["weight"]
        top_quantity = row["top_two"]["quantity"]
        lines.append(f"| {row['decision_date']} | {row['stocks_with_return']} / {row['universe_stocks']} | "
                     f"{row['quantity_action_reviewed_stocks']} | "
                     f"{shown(row['weight']['rank_ic'])} | {shown(row['quantity']['rank_ic'])} | "
                     f"{shown(top_weight.get('mean_net_excess_return'), True)} | "
                     f"{shown(top_quantity.get('mean_net_excess_return'), True)} |")
    lines += ["", "The September 2026 decision has no completed 30-day exit in this archive.",
              "Older disclosure cutoffs are provisional. Missing or unsafe return series are excluded",
              "and top-two cohorts are skipped when a selected stock lacks a return. These results",
              "are exploratory and do not establish predictive performance.", ""]
    output_path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return result
