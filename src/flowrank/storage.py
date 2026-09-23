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
    quantity REAL CHECK (quantity > 0),
    instrument_name TEXT,
    PRIMARY KEY (snapshot_id, asset_id)
);
CREATE TABLE IF NOT EXISTS share_actions (
    asset_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    share_multiplier REAL NOT NULL CHECK (share_multiplier > 0),
    description TEXT NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (asset_id, effective_date)
);
CREATE TABLE IF NOT EXISTS action_reviews (
    asset_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (asset_id, start_date, end_date)
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
    # Existing pilot databases predate share counts. Preserve their snapshots and
    # fill the new fields when the pinned importer is re-run.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(holdings)")}
    if "quantity" not in columns:
        db.execute("ALTER TABLE holdings ADD COLUMN quantity REAL CHECK (quantity > 0)")
    if "instrument_name" not in columns:
        db.execute("ALTER TABLE holdings ADD COLUMN instrument_name TEXT")
    return db


def _date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("published_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(path: str | Path, expected: set[str], optional: set[str] | None = None) -> list[dict[str, str]]:
    optional = optional or set()
    with open(path, newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        fields = set(reader.fieldnames or [])
        if not expected <= fields or fields - expected - optional:
            raise ValueError(f"Expected CSV columns: {', '.join(sorted(expected))}; optional: {', '.join(sorted(optional))}")
        rows = list(reader)
    if not rows:
        raise ValueError("CSV is empty")
    return rows


def import_holdings(db: sqlite3.Connection, path: str | Path) -> int:
    expected = {"scheme_id", "asset_id", "period_end", "published_at", "weight_pct", "source_url"}
    groups: dict[tuple[str, str, str, str], dict[str, tuple[float, float | None, str | None]]] = defaultdict(dict)
    sources: dict[tuple[str, str, str], str] = {}
    for row in _rows(path, expected, {"quantity", "instrument_name"}):
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
        quantity_text = (row.get("quantity") or "").strip()
        quantity = float(quantity_text) if quantity_text else None
        if quantity is not None and (not math.isfinite(quantity) or quantity <= 0):
            raise ValueError("quantity must be positive when provided")
        name = (row.get("instrument_name") or "").strip() or None
        identity = (scheme, period, published)
        if identity in sources and sources[identity] != source:
            raise ValueError(f"Conflicting source URLs within one snapshot: {scheme} {period}")
        sources[identity] = source
        key = (scheme, period, published, source)
        if asset in groups[key]:
            raise ValueError(f"Duplicate asset {asset} in one snapshot")
        groups[key][asset] = (weight, quantity, name)
    with db:
        for (scheme, period, published, source), holdings in groups.items():
            existing = db.execute(
                "SELECT id, source_url FROM holding_snapshots WHERE scheme_id=? AND period_end=? AND published_at=?",
                (scheme, period, published),
            ).fetchone()
            if existing:
                prior = {row["asset_id"]: row for row in db.execute(
                    "SELECT asset_id, weight_pct, quantity, instrument_name FROM holdings WHERE snapshot_id=?",
                    (existing["id"],))}
                if existing["source_url"] != source or set(prior) != set(holdings):
                    raise ValueError(f"Snapshot already exists with different content: {scheme} {period} {published}")
                for asset, (weight, quantity, name) in holdings.items():
                    old = prior[asset]
                    if (old["weight_pct"] != weight
                            or old["quantity"] is not None and quantity is not None and old["quantity"] != quantity
                            or old["instrument_name"] is not None and name is not None and old["instrument_name"] != name):
                        raise ValueError(f"Snapshot already exists with different content: {scheme} {period} {published}")
                    if old["quantity"] is None and quantity is not None or old["instrument_name"] is None and name is not None:
                        db.execute(
                            "UPDATE holdings SET quantity=COALESCE(quantity, ?), instrument_name=COALESCE(instrument_name, ?) "
                            "WHERE snapshot_id=? AND asset_id=?", (quantity, name, existing["id"], asset))
                continue
            cursor = db.execute(
                "INSERT INTO holding_snapshots(scheme_id, period_end, published_at, source_url) VALUES (?, ?, ?, ?)",
                (scheme, period, published, source),
            )
            db.executemany(
                "INSERT INTO holdings(snapshot_id, asset_id, weight_pct, quantity, instrument_name) VALUES (?, ?, ?, ?, ?)",
                ((cursor.lastrowid, asset, *values) for asset, values in holdings.items()),
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


def import_share_actions(db: sqlite3.Connection, path: str | Path) -> int:
    """Import verified share-count multipliers, such as 2.0 for a 1:1 bonus."""
    expected = {"asset_id", "effective_date", "share_multiplier", "description", "source_url"}
    rows = []
    seen = set()
    for row in _rows(path, expected):
        asset, description, source = (row[key].strip() for key in ("asset_id", "description", "source_url"))
        day = _date(row["effective_date"])
        factor = float(row["share_multiplier"])
        if not asset or not description or not source or not math.isfinite(factor) or factor <= 0:
            raise ValueError("Invalid share action")
        if (asset, day) in seen:
            raise ValueError(f"Duplicate share action: {asset} {day}")
        seen.add((asset, day))
        rows.append((asset, day, factor, description, source))
    with db:
        for asset, day, factor, description, source in rows:
            old = db.execute("SELECT share_multiplier, description, source_url FROM share_actions "
                             "WHERE asset_id=? AND effective_date=?", (asset, day)).fetchone()
            if old:
                if (old["share_multiplier"], old["description"], old["source_url"]) != (factor, description, source):
                    raise ValueError(f"Conflicting share action: {asset} {day}")
            else:
                db.execute("INSERT INTO share_actions VALUES (?, ?, ?, ?, ?)",
                           (asset, day, factor, description, source))
    return len(rows)


def import_action_reviews(db: sqlite3.Connection, path: str | Path) -> int:
    """Import documented periods checked for all quantity-changing actions."""
    expected = {"asset_id", "start_date", "end_date", "source_url"}
    rows = []
    seen = set()
    for row in _rows(path, expected):
        asset, source = row["asset_id"].strip(), row["source_url"].strip()
        start, end = _date(row["start_date"]), _date(row["end_date"])
        if not asset or not source or start > end:
            raise ValueError("Invalid action review")
        if (asset, start, end) in seen:
            raise ValueError(f"Duplicate action review: {asset} {start} {end}")
        seen.add((asset, start, end))
        rows.append((asset, start, end, source))
    with db:
        for asset, start, end, source in rows:
            old = db.execute("SELECT source_url FROM action_reviews WHERE asset_id=? AND start_date=? AND end_date=?",
                             (asset, start, end)).fetchone()
            if old:
                if old["source_url"] != source:
                    raise ValueError(f"Conflicting action review: {asset} {start} {end}")
            else:
                db.execute("INSERT INTO action_reviews VALUES (?, ?, ?, ?)", (asset, start, end, source))
    return len(rows)
