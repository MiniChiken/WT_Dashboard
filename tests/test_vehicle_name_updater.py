import csv
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import update_vehicle_names as updater


def localization(rows):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["<ID|readonly|noverify>", "<English>", "<French>"])
    writer.writerows(rows)
    return "\ufeff" + stream.getvalue()


class VehicleNameUpdaterTests(unittest.TestCase):
    def test_short_labels_preserve_punctuation_and_exclude_nonvehicles(self):
        catalog = {
            "cn_bt_5": {"unitMoveType": "tank"},
            "us_m4a2_1944_germ": {"unitMoveType": "tank"},
            "missing": {"unitMoveType": "air"},
            "human": {"unitMoveType": "human"},
            "economicRankMax": 42,
        }
        names, missing = updater.build_names(catalog, localization([
            ["cn_bt_5_0", "BT-5 mod. 1933", ""],
            ["cn_bt_5_1", "BT-5", ""],
            ["us_m4a2_1944_germ_1", "▀M4 748 (a)", ""],
            ["missing_shop", "Shop name; not a combat label", ""],
            ["human_1", "Human", ""],
        ]))
        self.assertEqual(names, {"cn_bt_5": "BT-5", "us_m4a2_1944_germ": "▀M4 748 (a)"})
        self.assertEqual(missing, ["missing"])

    def test_invalid_or_empty_sources_are_rejected(self):
        catalog = {"cn_bt_5": {"unitMoveType": "tank"}}
        for source in ("bad;header\n", localization([])):
            with self.subTest(source=source), self.assertRaises(ValueError):
                updater.build_names(catalog, source)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            updater.build_names(catalog, localization([
                ["cn_bt_5_1", "BT-5", ""],
                ["cn_bt_5_1", "Another vehicle", ""],
            ]))

    def test_refresh_pins_all_sources_and_records_provenance(self):
        commit = "a" * 40
        raw = localization([["cn_bt_5_1", "BT-5", ""]]).encode("utf-8")
        catalog = json.dumps({"cn_bt_5": {"unitMoveType": "tank"}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(updater, "OUTPUT_DIR", output), \
                    patch.object(updater, "fetch", side_effect=[raw, catalog, b"2.61.0.17\n"]) as fetch, \
                    patch("sys.argv", ["update_vehicle_names.py", "--ref", commit]):
                updater.main()
            self.assertTrue(all(f"/{commit}/" in call.args[0] for call in fetch.call_args_list))
            self.assertEqual(json.loads((output / "vehicle_names.json").read_text()), {"cn_bt_5": "BT-5"})
            metadata = json.loads((output / "vehicle_names.meta.json").read_text())
            self.assertEqual(metadata["commit"], commit)
            self.assertEqual(metadata["mapped_vehicle_count"], 1)
            self.assertEqual(metadata["missing_short_names"], [])

    def test_failed_download_keeps_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            existing = output / "vehicle_names.json"
            existing.write_text('{"known":"Known vehicle"}\n')
            with patch.object(updater, "OUTPUT_DIR", output), \
                    patch.object(updater, "fetch", side_effect=urllib.error.URLError("offline")), \
                    patch("sys.argv", ["update_vehicle_names.py", "--ref", "a" * 40]), \
                    self.assertRaises(urllib.error.URLError):
                updater.main()
            self.assertEqual(existing.read_text(), '{"known":"Known vehicle"}\n')

    def test_partial_localization_keeps_existing_snapshot(self):
        raw = localization([["cn_bt_5_1", "BT-5", ""]]).encode("utf-8")
        catalog = json.dumps({
            "cn_bt_5": {"unitMoveType": "tank"},
            "germ_jaguar_2": {"unitMoveType": "tank"},
        }).encode()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            existing = output / "vehicle_names.json"
            existing.write_text('{"known":"Known vehicle"}\n')
            with patch.object(updater, "OUTPUT_DIR", output), \
                    patch.object(updater, "fetch", side_effect=[raw, catalog, b"test"]), \
                    patch("sys.argv", ["update_vehicle_names.py", "--ref", "a" * 40]), \
                    self.assertRaisesRegex(ValueError, "95%"):
                updater.main()
            self.assertEqual(existing.read_text(), '{"known":"Known vehicle"}\n')


if __name__ == "__main__":
    unittest.main()
