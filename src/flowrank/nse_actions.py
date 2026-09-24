"""Review a pinned NSE corporate-action response for the HDFC pilot ISINs."""

import csv
import hashlib
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from .storage import import_action_reviews


NSE_ACTION_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
NSE_REPORT_PAGE = "https://www.nseindia.com/all-reports"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
NONCASH_WORDS = ("bonus", "split", "sub-division", "rights", "merger", "demerger",
                 "amalgamation", "consolidation", "arrangement", "scheme",
                 "buy back", "buyback", "spin-off", "capital reduction")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetch_pinned(source: dict, raw_dir: Path, action_api: bool) -> Path:
    target = raw_dir / source["local_name"]
    if target.exists():
        if _sha256(target) != source["sha256"]:
            raise ValueError(f"Cached NSE source has unexpected checksum: {target}")
        return target
    raw_dir.mkdir(parents=True, exist_ok=True)
    if action_api:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
                   "Referer": NSE_ACTION_PAGE}
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        with opener.open(Request(NSE_ACTION_PAGE, headers=headers), timeout=30):
            pass
    else:
        headers = {"User-Agent": USER_AGENT, "Referer": NSE_REPORT_PAGE}
        opener = None
    request = Request(source["url"], headers=headers)
    with (opener.open(request, timeout=30) if opener else urlopen(request, timeout=30)) as response:
        content = response.read()
    if hashlib.sha256(content).hexdigest() != source["sha256"]:
        raise ValueError(f"NSE source changed since it was pinned: {source['url']}")
    target.write_bytes(content)
    return target


def _listed_equity_isins(path: Path, listing_date: date) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(members) != 1:
            raise ValueError("Expected one NSE bhavcopy CSV")
        with archive.open(members[0]) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            if not {"TradDt", "SctySrs", "ISIN"} <= set(reader.fieldnames or []):
                raise ValueError("Unexpected NSE bhavcopy layout")
            rows = list(reader)
    if not rows or {row["TradDt"] for row in rows} != {listing_date.isoformat()}:
        raise ValueError("NSE bhavcopy date does not match catalog")
    return {row["ISIN"] for row in rows if row["SctySrs"] == "EQ"}


def build_action_review(db, catalog_path: Path, raw_dir: Path, output_dir: Path) -> dict:
    """Certify pilot ISINs only when the pinned NSE source shows cash events alone."""
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    start = date.fromisoformat(catalog["start_date"])
    end = date.fromisoformat(catalog["end_date"])
    listing_date = date.fromisoformat(catalog["listing_date"])
    if not start <= listing_date <= end:
        raise ValueError("Listing date must fall inside action review interval")
    action_path = _fetch_pinned(catalog["action_source"], raw_dir, True)
    listing_path = _fetch_pinned(catalog["listing_source"], raw_dir, False)
    listed = _listed_equity_isins(listing_path, listing_date)
    # Historical additions to the holdings warehouse are outside this pinned
    # July-August review. Include the June base snapshot and comparison months.
    first_snapshot = date(start.year, start.month, 1) - timedelta(days=1)
    assets = {row[0] for row in db.execute(
        "SELECT DISTINCT h.asset_id FROM holdings h JOIN holding_snapshots s ON s.id=h.snapshot_id "
        "WHERE h.quantity IS NOT NULL AND h.asset_id NOT LIKE 'IN9%' "
        "AND s.period_end>=? AND s.period_end<=?",
        (first_snapshot.isoformat(), end.isoformat()))}
    if not assets:
        raise ValueError("No imported holdings with share quantities")
    missing = assets - listed
    if missing:
        raise ValueError(f"Pilot ISINs missing from NSE EQ listing: {', '.join(sorted(missing))}")

    actions = json.loads(action_path.read_text(encoding="utf-8"))
    if not isinstance(actions, list) or not actions:
        raise ValueError("Empty or invalid NSE corporate-action response")
    matched = []
    unsupported = []
    for action in actions:
        if action.get("isin") not in assets:
            continue
        ex_date = datetime.strptime(action["exDate"], "%d-%b-%Y").date()
        if not start <= ex_date <= end:
            raise ValueError(f"NSE action outside requested period: {action}")
        subject = action["subject"].lower()
        matched.append(action)
        if "dividend" not in subject or any(word in subject for word in NONCASH_WORDS):
            unsupported.append(action)
    if unsupported:
        details = "; ".join(f"{row['isin']} {row['exDate']} {row['subject']}" for row in unsupported)
        raise ValueError(f"Share-changing or unclassified NSE actions require manual adjustment: {details}")

    output_dir.mkdir(parents=True, exist_ok=True)
    review_path = output_dir / "hdfc_action_reviews.csv"
    with review_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("asset_id", "start_date", "end_date", "source_url"))
        writer.writerows((asset, start.isoformat(), end.isoformat(), catalog["action_source"]["url"])
                         for asset in sorted(assets))
    count = import_action_reviews(db, review_path)
    manifest = {"reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "listing_date": listing_date.isoformat(), "reviewed_isins": count,
                "nse_actions_total": len(actions), "portfolio_cash_events": len(matched),
                "portfolio_share_changing_events": 0,
                "action_source": catalog["action_source"],
                "listing_source": catalog["listing_source"],
                "scope": "NSE EQ securities in this HDFC pilot; noncash events cause an error"}
    (output_dir / "hdfc_action_review_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
