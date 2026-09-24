import csv
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flowrank.evaluation import spearman
from flowrank.historical import build_historical_action_reviews, build_historical_prices
from flowrank.returns import _digest_rows
from flowrank.storage import connect


class HistoricalDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.output = self.root / "generated"
        self.output.mkdir()
        self.db = connect(self.root / "pilot.sqlite")
        self.days = ("2025-01-31", "2025-02-15", "2025-02-28")
        new = "INE123A01028"
        partial = "INE999A01010"
        self.db.execute("INSERT INTO holding_snapshots(scheme_id,period_end,published_at,source_url) "
                        "VALUES ('F','2025-01-31','2025-02-10T18:29:59+00:00','test://jan')")
        self.db.execute("INSERT INTO holding_snapshots(scheme_id,period_end,published_at,source_url) "
                        "VALUES ('F','2025-02-28','2025-03-10T18:29:59+00:00','test://feb')")
        self.db.execute("INSERT INTO holdings VALUES (1,?,2,100,'Example')", (new,))
        self.db.execute("INSERT INTO holdings VALUES (2,?,2,200,'Example')", (new,))
        self.db.commit()
        prices = [(new, self.days[0], "100"), (new, self.days[1], "49"),
                  (new, self.days[2], "50"), (partial, self.days[0], "20")]
        self.raw_csv = self.output / "nse_raw_closes.csv"
        with self.raw_csv.open("w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(("asset_id", "price_date", "raw_close", "source_url"))
            writer.writerows((asset, day, close, "test://bhavcopy") for asset, day, close in prices)
        manifest_days = []
        for day in self.days:
            archive = self.raw / f"{day}.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("bhavcopy.csv", "ISIN,TckrSymb,SctySrs\n"
                                f"{new},EXAMPLE,EQ\n"
                                + (f"{partial},PARTIAL,EQ\n" if day == self.days[0] else ""))
            manifest_days.append({"date": day, "sha256": self._sha(archive),
                                  "local_path": str(archive)})
        self.raw_manifest = self.output / "nse_raw_prices_manifest.json"
        self.raw_manifest.write_text(json.dumps({"start_date": self.days[0],
                                                 "end_date": self.days[-1],
                                                 "days": manifest_days,
                                                 "csv_sha256": self._sha(self.raw_csv)}))
        actions = [
            {"isin": "INE123A01010", "symbol": "EXAMPLE", "exDate": "15-Feb-2025",
             "subject": "Bonus 1:1"},
            {"isin": "INE123A01010", "symbol": "EXAMPLE", "exDate": "15-Feb-2025",
             "subject": "Dividend - Rs 1 Per Share/Special Dividend - Rs 1 Per Share"},
        ]
        action_file = self.raw / "actions.json"
        action_file.write_text(json.dumps(actions))
        tri = [{"Index Name": "Nifty 500", "Date": day, "TotalReturnsIndex": str(level)}
               for day, level in zip(("31 Jan 2025", "15 Feb 2025", "28 Feb 2025"),
                                     (1000, 1010, 1020))]
        tri_file = self.raw / "tri.json"
        tri_file.write_text(json.dumps(tri))
        self.catalog = self.root / "sources.json"
        self.catalog.write_text(json.dumps({
            "start_date": self.days[0], "end_date": self.days[-1],
            "holdings_review_end_date": self.days[-1], "benchmark_id": "NIFTY500_TRI",
            "action_source": {"local_name": "actions.json", "url": "test://actions",
                              "sha256": self._sha(action_file)},
            "tri_source": {"local_name": "tri.json", "url": "test://tri",
                           "canonical_sha256": _digest_rows(sorted(
                               (row["Date"], row["TotalReturnsIndex"]) for row in tri))},
            "nse_bhavcopy_archive_sha256": _digest_rows([
                (row["date"], row["sha256"]) for row in manifest_days]),
        }))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    @staticmethod
    def _sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_alias_bonus_and_two_dividends_adjust_return_and_quantity(self):
        review = build_historical_action_reviews(self.db, self.catalog, self.raw, self.output,
                                                 self.raw_csv, self.raw_manifest)
        prices = build_historical_prices(self.db, self.catalog, self.raw, self.output,
                                         self.raw_csv, self.raw_manifest)
        self.assertEqual(review["reviewed_asset_months"], 1)
        self.assertEqual(review["bonus_adjustments"], 1)
        self.assertEqual(prices["stock_isins"], 2)  # A partial price history is retained.
        self.assertEqual(self.db.execute("SELECT share_multiplier FROM share_actions").fetchone()[0], 2)
        adjusted = self.db.execute("SELECT adjusted_close FROM prices WHERE asset_id=? AND price_date=?",
                                   ("INE123A01028", "2025-02-15")).fetchone()[0]
        self.assertAlmostEqual(adjusted, 100)  # (49 * 2 + 1 + 1) / 100

    def test_spearman_uses_midranks_for_tied_scores(self):
        self.assertAlmostEqual(spearman([0, 0, 1, 1], [1, 2, 3, 4]), 0.8944271909999159)
        self.assertIsNone(spearman([0, 0, 0], [1, 2, 3]))

    def test_unsupported_action_under_old_isin_blocks_price_and_review(self):
        action_file = self.raw / "actions.json"
        action_file.write_text(json.dumps([{"isin": "INE123A01010", "symbol": "EXAMPLE",
                                            "exDate": "15-Feb-2025", "subject": "Buy Back"}]))
        catalog = json.loads(self.catalog.read_text())
        catalog["action_source"]["sha256"] = self._sha(action_file)
        self.catalog.write_text(json.dumps(catalog))
        review = build_historical_action_reviews(self.db, self.catalog, self.raw, self.output,
                                                 self.raw_csv, self.raw_manifest)
        prices = build_historical_prices(self.db, self.catalog, self.raw, self.output,
                                         self.raw_csv, self.raw_manifest)
        self.assertEqual(review["reviewed_asset_months"], 0)
        self.assertIn("INE123A01028", prices["excluded_isins"])
        self.assertEqual(len(prices["resolved_action_aliases"]), 1)


if __name__ == "__main__":
    unittest.main()
