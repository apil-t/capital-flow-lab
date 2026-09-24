"""Pinned HDFC monthly equity-portfolio pilot and its source audit trail."""

import csv
import hashlib
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .storage import import_holdings


PARSER_VERSION = "hdfc-listed-equity-v3"
IST = ZoneInfo("Asia/Kolkata")
ISIN = re.compile(r"^IN[A-Z0-9]{10}$")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
PORTFOLIO_PAGE = "https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio"
NOTICE_PAGE = "https://www.hdfcfund.com/statutory-disclosure/portfolio/notices-portfolio"
CSV_FIELDS = ("scheme_id", "asset_id", "period_end", "published_at", "weight_pct",
              "quantity", "instrument_name", "source_url")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _modified_day(value: str | None) -> date | None:
    if not value:
        return None
    return parsedate_to_datetime(value).astimezone(IST).date()


def _fetch(source: dict, raw_dir: Path, referer: str) -> dict:
    target = raw_dir / source["local_name"]
    expected = source["sha256"]
    if target.exists():
        if _sha256(target) != expected:
            raise ValueError(f"Cached source has unexpected checksum: {target}")
        return {"local_path": str(target), "bytes": target.stat().st_size,
                "sha256": expected, "retrieved_at": None, "http_last_modified": None,
                "cached": True}

    request = Request(source["url"], headers={"User-Agent": USER_AGENT, "Referer": referer})
    temporary = target.with_suffix(target.suffix + ".download")
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(request, timeout=30) as response, temporary.open("wb") as output:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status} for {source['url']}")
            modified = response.headers.get("Last-Modified")
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                output.write(chunk)
        observed = _sha256(temporary)
        if observed != expected:
            raise ValueError(f"Source changed since pilot was pinned: {source['url']}")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"local_path": str(target), "bytes": target.stat().st_size,
            "sha256": expected, "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "http_last_modified": modified, "cached": False}


def parse_equity_workbook(
    path: Path, scheme_name: str, period_end: str
) -> tuple[dict[str, tuple[float, float, str]], float]:
    """Read only the listed Equity block and reconcile it with its published subtotal."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("Install the pilot extra: python3 -m pip install -e '.[pilot]'") from exc

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        if any(extra.title != f"Derivative{sheet.title}" for extra in workbook.worksheets[1:]):
            raise ValueError(f"Unexpected additional worksheet in {path}")
        title = sheet.cell(1, 1).value
        reported = sheet.cell(2, 1).value
        expected_date = date.fromisoformat(period_end).strftime("%d-%b-%Y")
        if not isinstance(title, str) or not title.startswith(f"HDFC {scheme_name} Fund ("):
            raise ValueError(f"Unexpected scheme title in {path}: {title}")
        if reported != f"Portfolio as on {expected_date}":
            raise ValueError(f"Unexpected reporting date in {path}: {reported}")
        for row, column, expected in ((5, 2, "ISIN"), (5, 4, "Name Of the Instrument"),
                                      (5, 8, "% to NAV"), (6, 2, "EQUITY & EQUITY RELATED"),
                                      (7, 2, "(a) Listed / awaiting listing on Stock Exchanges"),
                                      (8, 2, "Equity")):
            if sheet.cell(row, column).value != expected:
                raise ValueError(f"Unexpected HDFC layout at {path}:{row},{column}")

        holdings: dict[str, tuple[float, float, str]] = {}
        subtotal = None
        for row in sheet.iter_rows(min_row=9, values_only=True):
            asset = row[1]
            if asset == "Sub Total":
                subtotal = row[7]
                break
            if not isinstance(asset, str) or not ISIN.fullmatch(asset):
                raise ValueError(f"Unexpected listed equity ISIN in {path}: {asset!r}")
            if asset in holdings:
                raise ValueError(f"Duplicate ISIN {asset} in {path}")
            weight = row[7]
            quantity = row[5]
            name = row[3]
            if weight == "@":
                # HDFC prints this marker for a holding below 0.01% of NAV.
                # Its share quantity is still usable; zero is the conservative
                # numeric representation of its rounded portfolio weight.
                weight = 0.0
            if (not isinstance(weight, (int, float)) or not math.isfinite(weight)
                    or weight < 0 or weight > 100):
                raise ValueError(f"Invalid weight for {asset} in {path}: {weight!r}")
            if not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
                raise ValueError(f"Invalid quantity for {asset} in {path}: {quantity!r}")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Missing instrument name for {asset} in {path}")
            holdings[asset] = (float(weight), float(quantity), name.strip())
        if not holdings or not isinstance(subtotal, (int, float)) or not math.isfinite(subtotal):
            raise ValueError(f"Missing listed equity holdings or subtotal in {path}")
        if any(weight == 0 for weight, _, _ in holdings.values()):
            if not any("@ Less than 0.01%." in str(row[1])
                       for row in sheet.iter_rows(values_only=True) if len(row) > 1):
                raise ValueError(f"Missing explanation of '@' weight marker in {path}")
        if abs(sum(value[0] for value in holdings.values()) - subtotal) > 0.02:
            raise ValueError(f"Listed equity subtotal mismatch in {path}")
        return holdings, float(subtotal)
    finally:
        workbook.close()


def build_pilot(db, catalog_path: Path, raw_dir: Path, output_dir: Path) -> dict:
    """Verify original files, write a normalized CSV, and import complete snapshots."""
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    rows = []
    audit = {"parser_version": PARSER_VERSION,
             "availability_basis": "end of later notice or HTTP Last-Modified date; when no notice is pinned, also wait 10 calendar days after month end; exact first-publication time unverified",
             "snapshots": []}
    seen = set()
    for period in catalog["periods"]:
        period_end = date.fromisoformat(period["period_end"])
        if "notice" in period:
            notice_date = date.fromisoformat(period["notice_date"])
            if notice_date <= period_end:
                raise ValueError(f"Notice does not follow reporting period: {period_end}")
            notice_file = _fetch(period["notice"], raw_dir, NOTICE_PAGE)
            notice_file["observed_http_last_modified"] = period["notice"].get("observed_http_last_modified")
            availability_basis = "notice_and_file_dates"
        elif period.get("availability_rule") == "file_last_modified_plus_10_calendar_days":
            notice_date = None
            notice_file = None
            availability_basis = "unverified_file_date_proxy"
        else:
            raise ValueError(f"Missing notice or supported availability rule for {period_end}")
        for source in period["sources"]:
            scheme_id = source["scheme_id"]
            key = (scheme_id, period_end.isoformat())
            if key in seen:
                raise ValueError(f"Duplicate catalog entry: {key}")
            seen.add(key)
            file_audit = _fetch(source, raw_dir, PORTFOLIO_PAGE)
            file_audit["observed_http_last_modified"] = source.get("observed_http_last_modified")
            file_dates = (_modified_day(file_audit["http_last_modified"]),
                          _modified_day(file_audit["observed_http_last_modified"]))
            if not notice_file and not any(file_dates):
                raise ValueError(f"No observed HTTP Last-Modified for {source['url']}")
            if notice_file:
                candidates = (notice_date,
                              _modified_day(notice_file["http_last_modified"]),
                              _modified_day(notice_file["observed_http_last_modified"]), *file_dates)
            else:
                candidates = (period_end + timedelta(days=10), *file_dates)
            availability_date = max(day for day in candidates if day is not None)
            published = datetime.combine(availability_date, time(23, 59, 59), IST).isoformat()
            holdings, subtotal = parse_equity_workbook(
                Path(file_audit["local_path"]), source["scheme_name"], period_end.isoformat()
            )
            for asset, (weight, quantity, name) in sorted(holdings.items()):
                rows.append({"scheme_id": scheme_id, "asset_id": asset,
                             "period_end": period_end.isoformat(), "published_at": published,
                             "weight_pct": f"{weight:.12g}", "quantity": f"{quantity:.15g}",
                             "instrument_name": name, "source_url": source["url"]})
            audit["snapshots"].append({"scheme_id": scheme_id, "period_end": period_end.isoformat(),
                                       "published_at": published, "availability_date": availability_date.isoformat(),
                                       "availability_basis": availability_basis,
                                       "notice_date": notice_date.isoformat() if notice_date else None,
                                       "notice_url": period["notice"]["url"] if notice_file else None,
                                       "notice_file": notice_file,
                                       "source_url": source["url"], "source_file": file_audit,
                                       "equity_count": len(holdings), "equity_subtotal_pct": subtotal})
    if not rows:
        raise ValueError("Pilot catalog contains no holdings")
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "hdfc_holdings.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    audit["holdings_count"] = len(rows)
    audit["snapshot_count"] = import_holdings(db, csv_path)
    (output_dir / "hdfc_manifest.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return audit
