#!/usr/bin/env python3
"""Discover and pin HDFC monthly workbooks for an inclusive month range.

Only months with all five validated scheme files enter the catalog. Missing
files and changed layouts are reported; the raw downloads stay in data/raw/.
"""

import argparse
import calendar
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from flowrank.hdfc import PORTFOLIO_PAGE, USER_AGENT, parse_equity_workbook


ROOT = Path(__file__).resolve().parents[1]
SCHEMES = (
    ("HDFC_FLEXI_CAP", "Flexi Cap", "flexi_cap"),
    ("HDFC_FOCUSED", "Focused", "focused"),
    ("HDFC_LARGE_CAP", "Large Cap", "large_cap"),
    ("HDFC_MID_CAP", "Mid Cap", "mid_cap"),
    ("HDFC_SMALL_CAP", "Small Cap", "small_cap"),
)


def months(start: str, end: str):
    first = date.fromisoformat(start + "-01")
    last = date.fromisoformat(end + "-01")
    if first > last:
        raise ValueError("--start must not follow --end")
    while first <= last:
        yield date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        first = date(first.year + (first.month == 12), first.month % 12 + 1, 1)


def source_url(period: date, scheme_name: str) -> str:
    next_month = date(period.year + (period.month == 12), period.month % 12 + 1, 1)
    filename = f"Monthly HDFC {scheme_name} Fund - {period.day} {period.strftime('%B %Y')}.xlsx"
    return f"https://files.hdfcfund.com/s3fs-public/{next_month:%Y-%m}/{quote(filename)}"


def names_for(period: date, scheme_id: str, current_name: str) -> tuple[str, ...]:
    # HDFC renamed these two schemes effective 27 June 2025. Try both aliases
    # near the transition and fail visibly if neither archive file exists.
    former = {"HDFC_FOCUSED": "Focused 30", "HDFC_MID_CAP": "Mid-Cap Opportunities"}.get(scheme_id)
    if former and period < date(2025, 6, 30):
        return former, current_name
    if former:
        return current_name, former
    return (current_name,)


def fetch(url: str, target: Path) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Referer": PORTFOLIO_PAGE})
    temporary = target.with_suffix(target.suffix + ".download")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(request, timeout=30) as response, temporary.open("wb") as output:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            modified = response.headers.get("Last-Modified")
            if not modified:
                raise ValueError("HTTP Last-Modified header missing")
            digest = hashlib.sha256()
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"url": url, "sha256": digest.hexdigest(),
            "observed_http_last_modified": modified, "local_name": target.name}


def discover(start: str, end: str, catalog_path: Path, raw_dir: Path,
             links_path: Path | None = None) -> dict:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    link_catalog = json.loads(links_path.read_text(encoding="utf-8")) if links_path else {"periods": {}}
    official_links = link_catalog["periods"]
    pinned = {period["period_end"] for period in catalog["periods"]}
    report = {"added": [], "already_pinned": [], "gaps": {}}
    for period in months(start, end):
        day = period.isoformat()
        if day in pinned:
            report["already_pinned"].append(day)
            continue
        sources = []
        failures = []
        for scheme_id, current_name, slug in SCHEMES:
            target = raw_dir / f"{day}_{slug}.xlsx"
            errors = []
            listed_url = official_links.get(day, {}).get(scheme_id)
            if listed_url:
                if urlsplit(listed_url).hostname != "files.hdfcfund.com":
                    raise ValueError(f"Unexpected source host for {day} {scheme_id}")
                candidates = [(current_name, listed_url)]
            else:
                candidates = [(name, source_url(period, name))
                              for name in names_for(period, scheme_id, current_name)]
            for name, url in candidates:
                try:
                    source = fetch(url, target)
                    parse_equity_workbook(target, name, day)
                except (HTTPError, URLError, OSError, ValueError) as exc:
                    target.unlink(missing_ok=True)
                    errors.append(f"{name}: {exc}")
                    continue
                source.update(scheme_id=scheme_id, scheme_name=name)
                if listed_url:
                    source["link_basis"] = link_catalog["source_page"]
                sources.append(source)
                break
            else:
                failures.append(f"{scheme_id}: {'; '.join(errors)}")
        if failures:
            report["gaps"][day] = failures
            print(f"{day}: incomplete ({len(sources)}/5 files)", file=sys.stderr)
            continue
        catalog["periods"].append({"period_end": day,
                                   "availability_rule": "file_last_modified_plus_10_calendar_days",
                                   "sources": sources})
        report["added"].append(day)
        print(f"{day}: pinned 5 validated workbooks")
    catalog["periods"].sort(key=lambda period: period["period_end"])
    if report["added"]:
        catalog["description"] = ("Pinned HDFC monthly listed-equity portfolios for five schemes; "
                                  "see each period for availability evidence")
        catalog_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="First reporting month, YYYY-MM")
    parser.add_argument("--end", required=True, help="Last reporting month, YYYY-MM")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/pilot/hdfc_sources.json")
    parser.add_argument("--links", type=Path, default=ROOT / "data/pilot/hdfc_archive_links.json",
                        help="Exact URLs observed on HDFC's monthly portfolio page")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw/hdfc")
    parser.add_argument("--report", type=Path, default=ROOT / "data/generated/hdfc_discovery_report.json")
    args = parser.parse_args()
    try:
        report = discover(args.start, args.end, args.catalog, args.raw_dir, args.links)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Added {len(report['added'])} months; {len(report['gaps'])} incomplete. Report: {args.report}")


if __name__ == "__main__":
    main()
