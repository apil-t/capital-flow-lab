"""Small SQLite warehouse with disclosure timestamps and complete scheme snapshots."""

import csv
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS holding_snapshots (
    id INTEGER PRIMARY KEY,
    scheme_id TEXT NOT NULL,
    period_end TEXT NOT NULL,
    published_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE (scheme_id, period_end, published_at)
);
CREATE TABLE IF NOT EXISTS holdings (
    snapshot_id INTEGER NOT NULL REFERENCES holding_snapshots(id),
    asset_id TEXT NOT NULL,
    weight_pct REAL NOT NULL CHECK (weight_pct >= 0 AND weight_pct <= 100),
    PRIMARY KEY (snapshot_id, asset_id)
);
CREATE TABLE IF NOT EXISTS prices (
    asset_id TEXT NOT NULL,
    price_date TEXT NOT NULL,
    adjusted_close REAL NOT NULL CHECK (adjusted_close > 0),
    source_url TEXT NOT NULL,
    PRIMARY KEY (asset_id, price_date)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_available
    ON holding_snapshots(published_at, period_end, scheme_id);
CREATE INDEX IF NOT EXISTS idx_prices_asset_date ON prices(asset_id, price_date);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("published_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(path: str | Path, expected: set[str]) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or set(reader.fieldnames) != expected:
            raise ValueError(f"Expected CSV columns: {', '.join(sorted(expected))}")
        rows = list(reader)
    if not rows:
        raise ValueError("CSV is empty")
    return rows


def import_holdings(db: sqlite3.Connection, path: str | Path) -> int:
    expected = {"scheme_id", "asset_id", "period_end", "published_at", "weight_pct", "source_url"}
    groups: dict[tuple[str, str, str, str], dict[str, float]] = defaultdict(dict)
    sources: dict[tuple[str, str, str], str] = {}
    for row in _rows(path, expected):
        scheme, asset, source = (row[key].strip() for key in ("scheme_id", "asset_id", "source_url"))
        if not scheme or not asset or not source:
            raise ValueError("scheme_id, asset_id and source_url are required")
        period = _date(row["period_end"])
        published = _timestamp(row["published_at"])
        if published[:10] < period:
            raise ValueError("published_at cannot precede period_end")
        weight = float(row["weight_pct"])
        if not math.isfinite(weight) or not 0 <= weight <= 100:
            raise ValueError("weight_pct must be between 0 and 100")
        identity = (scheme, period, published)
        if identity in sources and sources[identity] != source:
            raise ValueError(f"Conflicting source URLs within one snapshot: {scheme} {period}")
        sources[identity] = source
        key = (scheme, period, published, source)
        if asset in groups[key]:
            raise ValueError(f"Duplicate asset {asset} in one snapshot")
        groups[key][asset] = weight
    with db:
        for (scheme, period, published, source), holdings in groups.items():
            existing = db.execute(
                "SELECT id, source_url FROM holding_snapshots WHERE scheme_id=? AND period_end=? AND published_at=?",
                (scheme, period, published),
            ).fetchone()
            if existing:
                prior = {
                    row["asset_id"]: row["weight_pct"]
                    for row in db.execute("SELECT asset_id, weight_pct FROM holdings WHERE snapshot_id=?", (existing["id"],))
                }
                if existing["source_url"] != source or prior != holdings:
                    raise ValueError(f"Snapshot already exists with different content: {scheme} {period} {published}")
                continue
            cursor = db.execute(
                "INSERT INTO holding_snapshots(scheme_id, period_end, published_at, source_url) VALUES (?, ?, ?, ?)",
                (scheme, period, published, source),
            )
            db.executemany(
                "INSERT INTO holdings(snapshot_id, asset_id, weight_pct) VALUES (?, ?, ?)",
                ((cursor.lastrowid, asset, weight) for asset, weight in holdings.items()),
            )
    return len(groups)


def import_prices(db: sqlite3.Connection, path: str | Path) -> int:
    expected = {"asset_id", "price_date", "adjusted_close", "source_url"}
    rows = []
    seen = set()
    for row in _rows(path, expected):
        asset, source = row["asset_id"].strip(), row["source_url"].strip()
        if not asset or not source:
            raise ValueError("asset_id and source_url are required")
        day = _date(row["price_date"])
        close = float(row["adjusted_close"])
        if not math.isfinite(close) or close <= 0:
            raise ValueError("adjusted_close must be positive")
        if (asset, day) in seen:
            raise ValueError(f"Duplicate price: {asset} {day}")
        seen.add((asset, day))
        rows.append((asset, day, close, source))
    with db:
        for asset, day, close, source in rows:
            prior = db.execute(
                "SELECT adjusted_close, source_url FROM prices WHERE asset_id=? AND price_date=?",
                (asset, day),
            ).fetchone()
            if prior:
                if prior["adjusted_close"] != close or prior["source_url"] != source:
                    raise ValueError(f"Price already exists with different content: {asset} {day}")
            else:
                db.execute(
                    "INSERT INTO prices(asset_id, price_date, adjusted_close, source_url) VALUES (?, ?, ?, ?)",
                    (asset, day, close, source),
                )
    return len(rows)
