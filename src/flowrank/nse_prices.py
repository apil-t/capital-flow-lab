"""Archive official NSE bhavcopy closes by ISIN as unadjusted source data."""

import csv
import hashlib
import io
import json
import math
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
REPORT_PAGE = "https://www.nseindia.com/all-reports"


def bhavcopy_url(day: date) -> str:
    return ("https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip")


def parse_bhavcopy(content: bytes, day: date, assets: set[str]) -> dict[str, float]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"Expected one NSE bhavcopy CSV for {day}")
        with archive.open(members[0]) as source:
            reader = csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig"))
            required = {"TradDt", "SctySrs", "ISIN", "ClsPric"}
            if not required <= set(reader.fieldnames or []):
                raise ValueError(f"Unexpected NSE bhavcopy columns for {day}")
            rows = list(reader)
    if not rows or {row["TradDt"] for row in rows} != {day.isoformat()}:
        raise ValueError(f"NSE bhavcopy trade date mismatch: {day}")
    prices = {}
    for row in rows:
        asset = row["ISIN"]
        # A security can move from regular EQ trading to BE (trade-to-trade).
        # Keep both, but reject an ambiguous same-day ISIN close.
        if row["SctySrs"] not in {"EQ", "BE"} or asset not in assets:
            continue
        close = float(row["ClsPric"])
        if not math.isfinite(close) or close <= 0 or asset in prices:
            raise ValueError(f"Invalid or duplicate NSE EQ close for {asset} on {day}")
        prices[asset] = close
    return prices


def archive_raw_closes(db, start: date, end: date, raw_dir: Path, output_dir: Path) -> dict:
    """Download daily raw closes; keep them separate from adjusted backtest prices."""
    if start > end or (end - start).days > 1095 or end > date.today():
        raise ValueError("Choose a past date range of at most three years")
    assets = {row[0] for row in db.execute("SELECT DISTINCT asset_id FROM holdings WHERE quantity IS NOT NULL")}
    if not assets:
        raise ValueError("No imported HDFC ISINs with quantities")
    raw_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"start_date": start.isoformat(), "end_date": end.isoformat(),
                "price_basis": "unadjusted NSE EQ/BE bhavcopy close; not eligible for backtest import",
                "asset_count": len(assets), "days": [], "unavailable_dates": []}
    prior_path = output_dir / "nse_raw_prices_manifest.json"
    prior_hashes = ({row["date"]: row["sha256"] for row in json.loads(prior_path.read_text())["days"]}
                    if prior_path.exists() else {})
    output_rows = []
    day = start
    while day <= end:
        url = bhavcopy_url(day)
        target = raw_dir / Path(url).name
        if target.exists():
            content = target.read_bytes()
        else:
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Referer": REPORT_PAGE})
                with urlopen(request, timeout=30) as response:
                    content = response.read()
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                manifest["unavailable_dates"].append(day.isoformat())
                day += timedelta(days=1)
                continue
            # Validate the archive and its embedded date before caching it.
            parse_bhavcopy(content, day, assets)
            target.write_bytes(content)
        prices = parse_bhavcopy(content, day, assets)
        digest = hashlib.sha256(content).hexdigest()
        if day.isoformat() in prior_hashes and prior_hashes[day.isoformat()] != digest:
            raise ValueError(f"NSE bhavcopy checksum changed for {day}")
        for asset, close in sorted(prices.items()):
            output_rows.append((asset, day.isoformat(), f"{close:.12g}", url))
        manifest["days"].append({"date": day.isoformat(), "url": url,
                                 "local_path": str(target), "sha256": digest,
                                 "bytes": len(content), "matched_isins": len(prices)})
        day += timedelta(days=1)
    if not output_rows:
        raise ValueError("No NSE EQ prices found in requested range")
    csv_path = output_dir / "nse_raw_closes.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("asset_id", "price_date", "raw_close", "source_url"))
        writer.writerows(output_rows)
    manifest["rows"] = len(output_rows)
    manifest["csv_sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest["retrieved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (output_dir / "nse_raw_prices_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
