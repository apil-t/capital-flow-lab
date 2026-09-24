import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flowrank.nse_actions import build_action_review
from flowrank.nse_prices import parse_bhavcopy
from flowrank.storage import connect


class NseSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.db = connect(self.root / "test.sqlite")
        self.db.execute("INSERT INTO holding_snapshots(scheme_id, period_end, published_at, source_url) "
                        "VALUES ('FUND','2026-06-30','2026-07-08T18:29:59+00:00','test://fund')")
        self.db.execute("INSERT INTO holdings(snapshot_id, asset_id, weight_pct, quantity) "
                        "VALUES (1,'INE123456789',2,100)")
        self.db.execute("INSERT INTO holdings(snapshot_id, asset_id, weight_pct, quantity) "
                        "VALUES (1,'IN9397D01014',0.02,50)")
        self.db.execute("INSERT INTO holding_snapshots(scheme_id, period_end, published_at, source_url) "
                        "VALUES ('FUND','2025-01-31','2025-02-10T18:29:59+00:00','test://old')")
        self.db.execute("INSERT INTO holdings(snapshot_id, asset_id, weight_pct, quantity) "
                        "VALUES (2,'INE999999999',1,50)")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _catalog(self, subject):
        actions = [{"isin": "INE123456789", "exDate": "15-Jul-2026", "subject": subject}]
        action_path = self.raw / "actions.json"
        action_path.write_text(json.dumps(actions))
        listing_path = self.raw / "bhavcopy.zip"
        csv_bytes = ("TradDt,SctySrs,ISIN,ClsPric\n"
                     "2026-07-31,EQ,INE123456789,123.45\n").encode()
        with zipfile.ZipFile(listing_path, "w") as archive:
            archive.writestr("bhavcopy.csv", csv_bytes)
        catalog = {"start_date": "2026-07-01", "end_date": "2026-08-31",
                   "listing_date": "2026-07-31",
                   "action_source": {"local_name": "actions.json", "url": "test://actions",
                                     "sha256": hashlib.sha256(action_path.read_bytes()).hexdigest()},
                   "listing_source": {"local_name": "bhavcopy.zip", "url": "test://listing",
                                      "sha256": hashlib.sha256(listing_path.read_bytes()).hexdigest()}}
        path = self.root / "catalog.json"
        path.write_text(json.dumps(catalog))
        return path, listing_path.read_bytes()

    def test_dividend_only_review_and_price_parser(self):
        catalog, archive = self._catalog("Dividend - Rs 2 Per Share")
        result = build_action_review(self.db, catalog, self.raw, self.root / "generated")
        self.assertEqual(result["reviewed_isins"], 1)
        self.assertEqual(result["portfolio_cash_events"], 1)
        self.assertEqual(parse_bhavcopy(archive, date(2026, 7, 31), {"INE123456789"}),
                         {"INE123456789": 123.45})
        self.assertEqual(self.db.execute("SELECT count(*) FROM action_reviews").fetchone()[0], 1)

    def test_share_changing_event_requires_manual_action(self):
        catalog, _ = self._catalog("Bonus 1:1")
        with self.assertRaisesRegex(ValueError, "manual adjustment"):
            build_action_review(self.db, catalog, self.raw, self.root / "generated")
        self.assertEqual(self.db.execute("SELECT count(*) FROM action_reviews").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
