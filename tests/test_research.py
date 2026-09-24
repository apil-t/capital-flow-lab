import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flowrank.research import backtest, rank
from flowrank.storage import connect, import_holdings, import_prices


ROOT = Path(__file__).resolve().parents[1]


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / "test.sqlite")
        import_holdings(self.db, ROOT / "data/sample/holdings.csv")
        import_prices(self.db, ROOT / "data/sample/prices.csv")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_rank_uses_only_published_snapshots_and_ignores_later_revision(self):
        self.assertEqual(rank(self.db, date(2025, 2, 28)), [])
        before = rank(self.db, date(2025, 3, 31))
        self.assertEqual(before[0].asset_id, "TEST00000001")
        self.assertEqual(before[0].increasing_schemes, 2)

        # A correction to the February filing arrives in April. It cannot
        # alter what a researcher would have seen at the March decision date.
        revision = Path(self.temp.name) / "revision.csv"
        revision.write_text(
            "scheme_id,asset_id,period_end,published_at,weight_pct,source_url\n"
            "FUND_A,TEST00000001,2025-02-28,2025-04-05T12:00:00+05:30,1,synthetic://revision\n"
            "FUND_A,TEST00000002,2025-02-28,2025-04-05T12:00:00+05:30,9,synthetic://revision\n",
            encoding="utf-8",
        )
        import_holdings(self.db, revision)
        self.assertEqual(rank(self.db, date(2025, 3, 31)), before)

    def test_backtest_enters_after_decision_and_deducts_cost(self):
        results = backtest(
            self.db, date(2025, 3, 31), date(2025, 3, 31),
            horizon_days=30, top=1, benchmark="NIFTY500_SYNTH", cost_bps=20,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "evaluated")
        self.assertEqual(results[0]["assets"], ["TEST00000001"])
        self.assertAlmostEqual(results[0]["mean_gross_return"], 0.10)
        self.assertAlmostEqual(results[0]["mean_net_excess_return"], 0.078)

    def test_incomplete_cohort_is_skipped(self):
        self.db.execute("DELETE FROM prices WHERE asset_id='TEST00000001' AND price_date='2025-05-01'")
        self.db.commit()
        results = backtest(
            self.db, date(2025, 3, 31), date(2025, 3, 31),
            horizon_days=30, top=1, benchmark="NIFTY500_SYNTH", cost_bps=20,
        )
        self.assertEqual(results[0]["status"], "skipped")

    def test_missing_reporting_month_is_not_ranked_as_monthly_change(self):
        gap_db = connect(Path(self.temp.name) / "gap.sqlite")
        try:
            path = Path(self.temp.name) / "gap.csv"
            path.write_text(
                "scheme_id,asset_id,period_end,published_at,weight_pct,source_url\n"
                "FUND_X,TEST00000001,2025-05-31,2025-06-10T23:59:59+05:30,1,synthetic://may\n"
                "FUND_X,TEST00000001,2025-11-30,2025-12-10T23:59:59+05:30,5,synthetic://nov\n",
                encoding="utf-8",
            )
            import_holdings(gap_db, path)
            self.assertEqual(rank(gap_db, date(2025, 12, 15)), [])
        finally:
            gap_db.close()


if __name__ == "__main__":
    unittest.main()
