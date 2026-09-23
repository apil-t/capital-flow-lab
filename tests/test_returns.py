import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flowrank.nse_prices import parse_bhavcopy
from flowrank.returns import _digest_rows, build_return_prices
from flowrank.storage import connect


class ReturnPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.generated = self.root / "generated"
        self.generated.mkdir()
        self.db = connect(self.root / "pilot.sqlite")

        raw_rows = [
            ("ASSET_A", "2026-08-03", "100", "test://day1"),
            ("ASSET_A", "2026-08-04", "98", "test://day2"),
            ("ASSET_B", "2026-08-03", "50", "test://day1"),
            ("ASSET_B", "2026-08-04", "100", "test://day2"),
        ]
        self.raw_csv = self.generated / "nse_raw_closes.csv"
        with self.raw_csv.open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(("asset_id", "price_date", "raw_close", "source_url"))
            writer.writerows(raw_rows)
        days = []
        for day in ("2026-08-03", "2026-08-04"):
            archive = self.raw / f"{day}.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("source.csv", "source")
            days.append({"date": day, "sha256": self._sha(archive), "local_path": str(archive)})
        self.raw_manifest = self.generated / "nse_raw_prices_manifest.json"
        self.raw_manifest.write_text(json.dumps({
            "start_date": "2026-08-03", "end_date": "2026-08-04", "days": days,
            "csv_sha256": self._sha(self.raw_csv),
        }))

        action_path = self.raw / "actions.json"
        action_path.write_text(json.dumps([
            {"isin": "ASSET_A", "exDate": "04-Aug-2026", "subject": "Dividend - Rs 2 Per Share"},
            {"isin": "ASSET_B", "exDate": "04-Aug-2026", "subject": "Bonus 1:1"},
        ]))
        tri_path = self.raw / "tri.json"
        tri = [
            {"Date": "03 Aug 2026", "TotalReturnsIndex": "1000", "Index Name": "NIFTY 500"},
            {"Date": "04 Aug 2026", "TotalReturnsIndex": "1010", "Index Name": "NIFTY 500"},
        ]
        tri_path.write_text(json.dumps(tri))
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({
            "start_date": "2026-08-03", "end_date": "2026-08-04", "benchmark_id": "NIFTY500_TRI",
            "action_source": {"local_name": "actions.json", "url": "test://actions",
                              "sha256": self._sha(action_path)},
            "tri_source": {"local_name": "tri.json", "url": "test://tri",
                           "canonical_sha256": _digest_rows(sorted(
                               (row["Date"], row["TotalReturnsIndex"]) for row in tri))},
            "nse_bhavcopy_archive_sha256": _digest_rows([
                (row["date"], row["sha256"]) for row in days]),
        }))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    @staticmethod
    def _sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_cash_dividend_return_and_unsupported_action(self):
        result = build_return_prices(self.db, self.catalog, self.raw, self.generated,
                                     self.raw_csv, self.raw_manifest)
        self.assertEqual(result["price_rows"], 4)
        self.assertEqual(result["stock_isins"], 1)
        self.assertIn("ASSET_B", result["excluded_isins"])
        values = {(row["asset_id"], row["price_date"]): row["adjusted_close"]
                  for row in self.db.execute("SELECT * FROM prices")}
        self.assertEqual(values[("ASSET_A", "2026-08-03")], 100)
        self.assertEqual(values[("ASSET_A", "2026-08-04")], 100)
        self.assertEqual(values[("NIFTY500_TRI", "2026-08-04")], 1010)
        self.assertFalse(any(asset == "ASSET_B" for asset, _ in values))

    def test_source_hash_mismatch_rejected(self):
        self.raw_csv.write_text(self.raw_csv.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            build_return_prices(self.db, self.catalog, self.raw, self.generated,
                                self.raw_csv, self.raw_manifest)

    def test_eq_to_be_series_transition(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("source.csv", "TradDt,SctySrs,ISIN,ClsPric\n"
                             "2026-08-04,BE,ASSET_A,98\n")
        self.assertEqual(parse_bhavcopy(buffer.getvalue(), date(2026, 8, 4), {"ASSET_A"}),
                         {"ASSET_A": 98})


if __name__ == "__main__":
    unittest.main()
