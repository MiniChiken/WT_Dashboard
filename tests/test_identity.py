import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analysis
import db
from poller import TelemetryPoller


class VehicleIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch("db.DB_PATH", Path(directory.name) / "stats.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        self.tracker = TelemetryPoller()
        with patch("poller.time.time", return_value=1000):
            self.tracker._handle_match_transition(
                False, True, {"army": "air", "type": "dummy_plane"},
            )

    def vehicle(self, seconds, vehicle_type):
        self.tracker.latest["indicators"] = {"army": "tank", "type": vehicle_type}
        with patch("poller.time.time", return_value=1000 + seconds):
            self.tracker._track_own_vehicle()

    def event(self, seconds, name, vehicle):
        with patch("poller.time.time", return_value=1000 + seconds):
            self.tracker._maybe_resolve_my_name(
                {"actor_name": name, "actor_vehicle": vehicle}, seconds,
            )

    def test_internal_ids_match_recorded_display_names(self):
        for raw, display in (
            ("tankModels/germ_spz_oerlikon_raketenautomat", "Raketenautomat"),
            ("tankModels/germ_jaguar_2", "Jaguar 2"),
            ("tankModels/germ_leopard_I_a1", "Leopard A1A1"),
            ("tankModels/germ_wiesel_1_mk20", "Wiesel 1A4"),
            ("tankModels/germ_mkpz_m48a2ga2", "M48A2 G A2"),
            ("tankModels/germ_test_123", "Test 123"),
            ("tankModels/ussr_bmd_4m", "BMD-4M"),
            ("tankModels/fr_amx_30", "AMX-30"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(analysis.normalize_vehicle_id(raw), analysis.normalize_vehicle_name(display))
        self.assertEqual(analysis.normalize_vehicle_id("dummy_plane"), "")

    def test_header_uses_first_real_vehicle_and_keeps_it_after_respawn(self):
        session_id = self.tracker._session_id
        self.assertIsNone(db.get_session(session_id)["session"]["vehicle_type"])
        self.vehicle(1, "dummy_plane")
        self.assertFalse(self.tracker._own_vehicle_intervals)
        self.vehicle(4, "tankModels/germ_wiesel_1_mk20")
        self.vehicle(20, "tankModels/germ_jaguar_2")
        session = db.get_session(session_id)["session"]
        self.assertEqual(session["army"], "tank")
        self.assertEqual(session["vehicle_type"], "tankModels/germ_wiesel_1_mk20")

    def test_matching_vehicle_resolves_identity_after_switch(self):
        self.vehicle(2, "tankModels/germ_jaguar_2")
        self.event(10, "local player", "Jaguar 2")
        self.assertIsNone(self.tracker._my_name)
        self.vehicle(20, "tankModels/germ_wiesel_1_mk20")
        self.assertEqual(db.get_session_my_name(self.tracker._session_id), "local player")

    def test_same_vehicle_teammate_cannot_be_confirmed_as_local_player(self):
        self.vehicle(2, "tankModels/germ_wiesel_1_mk20")
        self.event(10, "teammate", "Wiesel 1A4")
        self.assertEqual(db.get_session_provisional_my_name(self.tracker._session_id), "teammate")
        self.event(12, "local player", "Wiesel 1A4")
        self.vehicle(20, "tankModels/germ_jaguar_2")
        self.assertIsNone(db.get_session_my_name(self.tracker._session_id))
        self.assertIsNone(db.get_session_provisional_my_name(self.tracker._session_id))

    def test_each_nation_can_resolve_local_player(self):
        for unit_id, name in (
            ("us_m4a1_1942_sherman", "M4A1"),
            ("ussr_bt_5", "BT-5"),
            ("germ_jaguar_2", "Jaguar 2"),
            ("uk_a_13_mk1", "A13 Mk I"),
            ("jp_type_75", "Type 75 SPH"),
            ("cn_bt_5", "BT-5"),
            ("it_m15_42", "M15/42"),
            ("fr_b1_ter", "B1 ter"),
            ("sw_strv_m31", "Strv m/31"),
            ("il_magach_6b_gal_batash", "Gal Batash"),
        ):
            with self.subTest(unit_id=unit_id):
                self.tracker = TelemetryPoller()
                with patch("poller.time.time", return_value=1000):
                    self.tracker._handle_match_transition(False, True, {"army": "tank", "type": "dummy_plane"})
                self.vehicle(2, f"tankModels/{unit_id}")
                self.event(10, "local player", name)
                self.vehicle(20, "unlisted_replacement_vehicle")
                self.assertEqual(db.get_session_my_name(self.tracker._session_id), "local player")

    def test_imported_vehicle_still_requires_an_unambiguous_player(self):
        # Chinese and Soviet BT-5s share a short label. Matching a localized
        # name must not bypass the existing same-vehicle ambiguity checks.
        self.vehicle(2, "tankModels/cn_bt_5")
        self.event(10, "teammate", "BT-5")
        self.event(12, "local player", "BT-5")
        self.vehicle(20, "tankModels/ussr_bt_5")
        self.assertIsNone(db.get_session_my_name(self.tracker._session_id))
        self.assertIsNone(db.get_session_provisional_my_name(self.tracker._session_id))


if __name__ == "__main__":
    unittest.main()
