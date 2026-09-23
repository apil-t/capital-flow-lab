import csv
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flowrank.research import backtest, disclosure_dates, quantity_rank, rank
from flowrank.storage import (connect, import_action_reviews, import_holdings,
                              import_prices, import_share_actions)


class QuantityResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = connect(self.root / "research.sqlite")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _csv(self, name, columns, rows):
        path = self.root / name
        with path.open("w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            writer.writerows(rows)
        return path

    def _holdings(self):
        columns = ("scheme_id", "asset_id", "period_end", "published_at", "weight_pct",
                   "quantity", "instrument_name", "source_url")
        rows = [
            ("FUND", "ASSET_A", "2026-06-30", "2026-07-08T23:59:59+05:30", 2, 100, "Alpha", "test://june"),
            ("FUND", "ASSET_B", "2026-06-30", "2026-07-08T23:59:59+05:30", 3, 100, "Beta", "test://june"),
            ("FUND", "ASSET_A", "2026-07-31", "2026-08-08T23:59:59+05:30", 3, 80, "Alpha", "test://july"),
            ("FUND", "ASSET_B", "2026-07-31", "2026-08-08T23:59:59+05:30", 2, 220, "Beta", "test://july"),
        ]
        import_holdings(self.db, self._csv("holdings.csv", columns, rows))

    def test_share_signal_needs_review_and_adjusts_split(self):
        self._holdings()
        self.assertEqual(rank(self.db, date(2026, 8, 8))[0].asset_id, "ASSET_A")
        self.assertEqual(quantity_rank(self.db, date(2026, 8, 8)), [])
        exploratory = quantity_rank(self.db, date(2026, 8, 8), allow_unreviewed_actions=True)
        self.assertEqual(exploratory[0].asset_id, "ASSET_B")
        self.assertEqual(exploratory[0].net_share_change, 120)
        self.assertEqual(exploratory[0].unreviewed_pairs, 1)

        actions = self._csv("actions.csv", ("asset_id", "effective_date", "share_multiplier",
                                           "description", "source_url"),
                            [("ASSET_B", "2026-07-15", 2, "one-for-one bonus", "test://action")])
        import_share_actions(self.db, actions)
        reviews = self._csv("reviews.csv", ("asset_id", "start_date", "end_date", "source_url"),
                            [(asset, "2026-07-01", "2026-07-31", "test://review")
                             for asset in ("ASSET_A", "ASSET_B")])
        import_action_reviews(self.db, reviews)
        checked = quantity_rank(self.db, date(2026, 8, 8))
        self.assertEqual(checked[0].asset_id, "ASSET_B")
        self.assertEqual(checked[0].net_share_change, 20)
        self.assertEqual(checked[0].unreviewed_pairs, 0)
        self.assertEqual(checked[1].asset_id, "ASSET_A")
        self.assertEqual(checked[1].net_share_change, -20)

    def test_disclosure_backtest_enters_after_cutoff_and_compares_signals(self):
        self._holdings()
        import_action_reviews(self.db, self._csv(
            "reviews.csv", ("asset_id", "start_date", "end_date", "source_url"),
            [(asset, "2026-07-01", "2026-07-31", "test://review") for asset in ("ASSET_A", "ASSET_B")]))
        prices = [(asset, day, close, "test://prices") for asset, values in {
            "ASSET_A": [("2026-08-10", 100), ("2026-08-20", 110)],
            "ASSET_B": [("2026-08-10", 100), ("2026-08-20", 120)],
            "INDEX": [("2026-08-10", 1000), ("2026-08-20", 1050)],
        }.items() for day, close in values]
        import_prices(self.db, self._csv("prices.csv", ("asset_id", "price_date", "adjusted_close", "source_url"), prices))
        self.assertEqual(disclosure_dates(self.db, date(2026, 8, 1), date(2026, 8, 31)), [date(2026, 8, 8)])
        self.assertEqual(rank(self.db, date(2026, 8, 7)), [])
        weight = backtest(self.db, date(2026, 8, 1), date(2026, 8, 31), 10, 1, "INDEX", 20,
                          schedule="disclosure")
        quantity = backtest(self.db, date(2026, 8, 1), date(2026, 8, 31), 10, 1, "INDEX", 20,
                            signal="quantity", schedule="disclosure")
        self.assertEqual(weight[0]["assets"], ["ASSET_A"])
        self.assertEqual(quantity[0]["assets"], ["ASSET_B"])
        self.assertAlmostEqual(weight[0]["mean_net_excess_return"], 0.048)
        self.assertAlmostEqual(quantity[0]["mean_net_excess_return"], 0.148)

    def test_existing_database_accepts_quantity_backfill(self):
        old_db = self.root / "old.sqlite"
        with sqlite3.connect(old_db) as db:
            db.execute("CREATE TABLE holdings (snapshot_id INTEGER, asset_id TEXT, weight_pct REAL)")
        with connect(old_db) as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(holdings)")}
            self.assertTrue({"quantity", "instrument_name"} <= columns)

    def test_missing_quantity_does_not_create_false_new_position(self):
        self._holdings()
        self.db.execute("UPDATE holdings SET quantity=NULL WHERE snapshot_id=1 AND asset_id='ASSET_B'")
        import_action_reviews(self.db, self._csv(
            "reviews.csv", ("asset_id", "start_date", "end_date", "source_url"),
            [(asset, "2026-07-01", "2026-07-31", "test://review")
             for asset in ("ASSET_A", "ASSET_B")]))
        checked = quantity_rank(self.db, date(2026, 8, 8))
        self.assertEqual([row.asset_id for row in checked], ["ASSET_A"])


if __name__ == "__main__":
    unittest.main()
