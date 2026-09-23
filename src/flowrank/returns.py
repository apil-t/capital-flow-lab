"""Build a tightly pinned total-return price pilot from public NSE sources."""

import csv
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from .nse_actions import NONCASH_WORDS, _fetch_pinned
from .storage import import_prices


DIVIDEND_AMOUNT = re.compile(r"\b(?:Rs|Re)\.?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


def _digest_rows(rows) -> str:
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _raw_closes(raw_csv: Path, raw_manifest: Path, catalog: dict) -> dict[str, list[tuple[date, float, str]]]:
    manifest = json.loads(raw_manifest.read_text(encoding="utf-8"))
    if (manifest["start_date"] > catalog["start_date"] or
            manifest["end_date"] < catalog["end_date"]):
        raise ValueError("NSE raw price archive does not cover return pilot")
    if hashlib.sha256(raw_csv.read_bytes()).hexdigest() != manifest["csv_sha256"]:
        raise ValueError("NSE raw close CSV checksum mismatch")
    relevant = [row for row in manifest["days"]
                if catalog["start_date"] <= row["date"] <= catalog["end_date"]]
    if _digest_rows([(row["date"], row["sha256"]) for row in relevant]) != catalog["nse_bhavcopy_archive_sha256"]:
        raise ValueError("NSE bhavcopy archive digest does not match pinned pilot")
    for row in relevant:
        if hashlib.sha256(Path(row["local_path"]).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"NSE source archive checksum mismatch: {row['date']}")
    closes = defaultdict(list)
    seen = set()
    with raw_csv.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if set(reader.fieldnames or []) != {"asset_id", "price_date", "raw_close", "source_url"}:
            raise ValueError("Unexpected NSE raw close CSV columns")
        for row in reader:
            day = date.fromisoformat(row["price_date"])
            if not catalog["start_date"] <= day.isoformat() <= catalog["end_date"]:
                continue
            asset = row["asset_id"]
            price = float(row["raw_close"])
            if (asset, day) in seen or not math.isfinite(price) or price <= 0:
                raise ValueError(f"Duplicate or invalid raw close: {asset} {day}")
            seen.add((asset, day))
            closes[asset].append((day, price, row["source_url"]))
    if not closes:
        raise ValueError("No raw closes inside return pilot range")
    return closes


def _tri_rows(source: dict, raw_dir: Path, start: date, end: date) -> list[tuple[date, float]]:
    target = raw_dir / source["local_name"]
    if not target.exists():
        raw_dir.mkdir(parents=True, exist_ok=True)
        request = Request(
            source["url"], data=json.dumps({"cinfo": source["cinfo"]}).encode(),
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8",
                     "X-Requested-With": "XMLHttpRequest", "Referer": source["page_url"]}
        )
        with urlopen(request, timeout=30) as response:
            target.write_bytes(response.read())
    rows = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("Empty Nifty TRI response")
    canonical = sorted((row["Date"], row["TotalReturnsIndex"]) for row in rows)
    if _digest_rows(canonical) != source["canonical_sha256"]:
        raise ValueError("Nifty TRI series changed since it was pinned")
    output = []
    seen = set()
    for row in rows:
        if row["Index Name"].upper() != "NIFTY 500":
            raise ValueError("Unexpected Nifty TRI index")
        day = datetime.strptime(row["Date"], "%d %b %Y").date()
        value = float(row["TotalReturnsIndex"])
        if day in seen or not start <= day <= end or not math.isfinite(value) or value <= 0:
            raise ValueError("Invalid Nifty TRI row")
        seen.add(day)
        output.append((day, value))
    return sorted(output)


def build_return_prices(db, catalog_path: Path, raw_dir: Path, output_dir: Path,
                        raw_csv: Path, raw_manifest: Path) -> dict:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    start, end = date.fromisoformat(catalog["start_date"]), date.fromisoformat(catalog["end_date"])
    closes = _raw_closes(raw_csv, raw_manifest, catalog)
    action_path = _fetch_pinned(catalog["action_source"], raw_dir, True)
    actions = json.loads(action_path.read_text(encoding="utf-8"))
    if not isinstance(actions, list) or not actions:
        raise ValueError("Empty NSE corporate-action response")
    tri = _tri_rows(catalog["tri_source"], raw_dir, start, end)
    if {day for day, _ in tri} != {day for values in closes.values() for day, _, _ in values}:
        raise ValueError("Nifty TRI days differ from NSE raw-price days")

    dividends = defaultdict(float)
    unsupported = defaultdict(list)
    for action in actions:
        asset = action.get("isin")
        if asset not in closes:
            continue
        day = datetime.strptime(action["exDate"], "%d-%b-%Y").date()
        if not start <= day <= end:
            raise ValueError(f"NSE corporate action outside pilot range: {action}")
        subject = action["subject"]
        lower_subject = subject.lower()
        if "dividend" not in lower_subject or any(word in lower_subject for word in NONCASH_WORDS):
            unsupported[asset].append(f"{day}: {subject}")
            continue
        amounts = DIVIDEND_AMOUNT.findall(subject)
        if len(amounts) != 1:
            unsupported[asset].append(f"{day}: unparsed {subject}")
            continue
        amount = float(amounts[0])
        if not math.isfinite(amount) or amount <= 0:
            unsupported[asset].append(f"{day}: invalid {subject}")
            continue
        dividends[(asset, day)] += amount

    output_rows = []
    excluded = dict(unsupported)
    trading_days = {day for day, _ in tri}
    for asset, values in sorted(closes.items()):
        if asset in excluded:
            continue
        values.sort()
        available = {day for day, _, _ in values}
        if available != trading_days:
            excluded[asset] = [f"missing {len(trading_days - available)} NSE trading-day closes"]
            continue
        first, last = values[0][0], values[-1][0]
        missing_ex_dates = [day for isin, day in dividends
                            if isin == asset and first < day <= last and day not in available]
        if missing_ex_dates:
            excluded[asset] = [f"dividend ex-date missing close: {day}" for day in missing_ex_dates]
            continue
        adjusted = values[0][1]
        prior_close = values[0][1]
        for i, (day, close, source_url) in enumerate(values):
            if i:
                adjusted *= (close + dividends[(asset, day)]) / prior_close
                prior_close = close
            output_rows.append((asset, day.isoformat(), f"{adjusted:.12g}", source_url))
    for day, value in tri:
        output_rows.append((catalog["benchmark_id"], day.isoformat(), f"{value:.12g}",
                            catalog["tri_source"]["url"]))
    output_rows.sort()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_csv = output_dir / "pilot_total_return_prices.csv"
    with output_csv.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("asset_id", "price_date", "adjusted_close", "source_url"))
        writer.writerows(output_rows)
    count = import_prices(db, output_csv)
    manifest = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "benchmark_id": catalog["benchmark_id"], "price_rows": count,
                "trading_days": len(trading_days),
                "stock_isins": len(closes) - len(excluded), "excluded_isins": excluded,
                "cash_dividend_events": sum(1 for amount in dividends.values() if amount > 0),
                "source_catalog": str(catalog_path), "raw_price_manifest": str(raw_manifest),
                "price_csv_sha256": hashlib.sha256(output_csv.read_bytes()).hexdigest(),
                "method": "NSE EQ close compounded with cash dividend on ex-date; exclude other actions; Nifty 500 official TRI"}
    (output_dir / "pilot_total_return_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
