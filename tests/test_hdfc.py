import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flowrank.hdfc import build_pilot, parse_equity_workbook
from flowrank.research import rank
from flowrank.storage import connect

try:
    from openpyxl import Workbook
except ImportError:
    Workbook = None


@unittest.skipIf(Workbook is None, "openpyxl pilot extra is not installed")
class HdfcPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.db = connect(self.root / "pilot.sqlite")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _workbook(self, name: str, period: str, weight: float, subtotal: float | None = None) -> dict:
        workbook = Workbook()
        sheet = workbook.active
        sheet["A1"] = "HDFC Focused Fund (test disclosure)"
        sheet["A2"] = "Portfolio as on " + date.fromisoformat(period).strftime("%d-%b-%Y")
        sheet["B5"] = "ISIN"
        sheet["D5"] = "Name Of the Instrument"
        sheet["H5"] = "% to NAV"
        sheet["B6"] = "EQUITY & EQUITY RELATED"
        sheet["B7"] = "(a) Listed / awaiting listing on Stock Exchanges"
        sheet["B8"] = "Equity"
        sheet["B9"] = "INE123456789"
        sheet["D9"] = "Test Company Limited"
        sheet["F9"] = 100
        sheet["H9"] = weight
        sheet["B10"] = "Sub Total"
        sheet["H10"] = weight if subtotal is None else subtotal
        sheet["B11"] = "INE987654321"  # an unrelated security after the equity block
        sheet["H11"] = 20
        path = self.raw / name
        workbook.save(path)
        return {"scheme_id": "HDFC_FOCUSED", "scheme_name": "Focused", "local_name": name,
                "url": f"https://example.test/{name}", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def _period(self, period: str, notice_date: str, source: dict) -> dict:
        name = f"{period}.pdf"
        path = self.raw / name
        path.write_bytes(b"test notice")
        return {"period_end": period, "notice_date": notice_date,
                "notice": {"local_name": name, "url": f"https://example.test/{name}",
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
                "sources": [source]}

    def test_import_reconciles_equity_and_respects_notice_dates(self):
        june = self._workbook("june.xlsx", "2026-06-30", 2.0)
        july = self._workbook("july.xlsx", "2026-07-31", 3.0)
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps({"periods": [
            self._period("2026-06-30", "2026-07-08", june),
            self._period("2026-07-31", "2026-08-08", july),
        ]}))
        audit = build_pilot(self.db, catalog, self.raw, self.root / "output")
        self.assertEqual((audit["snapshot_count"], audit["holdings_count"]), (2, 2))
        saved = self.db.execute("SELECT quantity, instrument_name FROM holdings LIMIT 1").fetchone()
        self.assertEqual((saved["quantity"], saved["instrument_name"]), (100, "Test Company Limited"))
        self.assertEqual(rank(self.db, date(2026, 8, 7)), [])
        self.assertEqual(rank(self.db, date(2026, 8, 8))[0].asset_id, "INE123456789")
        self.assertEqual(rank(self.db, date(2026, 8, 8))[0].increasing_schemes, 1)
        self.assertEqual(build_pilot(self.db, catalog, self.raw, self.root / "output")["snapshot_count"], 2)

    def test_rejects_wrong_equity_subtotal_and_changed_source(self):
        source = self._workbook("bad.xlsx", "2026-06-30", 2.0, subtotal=3.0)
        with self.assertRaisesRegex(ValueError, "subtotal mismatch"):
            parse_equity_workbook(self.raw / "bad.xlsx", "Focused", "2026-06-30")
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps({"periods": [self._period("2026-06-30", "2026-07-08", source)]}))
        (self.raw / "bad.xlsx").write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "unexpected checksum"):
            build_pilot(self.db, catalog, self.raw, self.root / "output")
        self.assertEqual(self.db.execute("SELECT count(*) FROM holding_snapshots").fetchone()[0], 0)

    def test_tiny_published_weight_keeps_quantity(self):
        source = self._workbook("tiny.xlsx", "2026-05-31", 0)
        path = self.raw / source["local_name"]
        from openpyxl import load_workbook
        book = load_workbook(path)
        sheet = book.active
        sheet["H9"] = "@"
        sheet["B12"] = "@ Less than 0.01%."
        book.save(path)
        holdings, subtotal = parse_equity_workbook(path, "Focused", "2026-05-31")
        self.assertEqual((holdings["INE123456789"][0], holdings["INE123456789"][1], subtotal),
                         (0, 100, 0))

    def test_notice_free_month_uses_conservative_date_proxy(self):
        source = self._workbook("may.xlsx", "2026-05-31", 2.0)
        source["observed_http_last_modified"] = "Fri, 05 Jun 2026 00:00:00 GMT"
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps({"periods": [{
            "period_end": "2026-05-31",
            "availability_rule": "file_last_modified_plus_10_calendar_days",
            "sources": [source],
        }]}))
        audit = build_pilot(self.db, catalog, self.raw, self.root / "output")
        self.assertEqual(audit["snapshots"][0]["availability_date"], "2026-06-10")
        self.assertEqual(audit["snapshots"][0]["availability_basis"], "unverified_file_date_proxy")


if __name__ == "__main__":
    unittest.main()
