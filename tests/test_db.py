import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from pointsranker import db, teams

PRED = {
    "id": "65a30fdc-c05e-4795-8ad4-4bc9e824cfde",
    "title": "M203; Odin's Call vs The Skeleton Crew (0-6)",
    "status": "ACTIVE",
    "createdAt": "2026-09-13T21:23:01Z", "lockedAt": None, "endedAt": None,
    "predictionWindowSeconds": 300,
    "outcomes": [
        {"id": "o1", "title": "The Skeleton Crew (0-6)", "color": "BLUE", "totalPoints": 1000, "totalUsers": 10},
        {"id": "o2", "title": "Odin's Call ", "color": "PINK", "totalPoints": 2000, "totalUsers": 20},
    ],
    "winningOutcome": None,
}


class UpsertTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")

    def upsert(self, pred):
        return db.upsert_prediction(self.conn, "chan", pred, {"o1": None, "o2": None}, "stream1")

    def count(self, table):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_idempotent_and_snapshots_only_on_change(self):
        self.assertEqual(self.upsert(PRED), (None, True))
        self.assertEqual(self.upsert(PRED), ("ACTIVE", False))
        self.assertEqual((self.count("predictions"), self.count("outcomes"), self.count("outcome_snapshots")), (1, 2, 2))

        resolved = {**PRED, "status": "RESOLVED", "winningOutcome": {"id": "o2"},
                    "outcomes": [PRED["outcomes"][0], {**PRED["outcomes"][1], "totalPoints": 2500}]}
        self.assertEqual(self.upsert(resolved), ("ACTIVE", True))
        self.assertEqual(self.count("outcome_snapshots"), 3)
        row = self.conn.execute("SELECT status, winning_outcome_id, stream_id FROM predictions").fetchone()
        self.assertEqual(tuple(row), ("RESOLVED", "o2", "stream1"))


class SideTest(unittest.TestCase):
    def test_parse_and_assign(self):
        sides = teams.parse_sides("14:30|M73; 34-yrs-old men vs ZERG REBORN (2-1)")
        self.assertEqual(sides, ["34-yrs-old men", "ZERG REBORN"])
        self.assertEqual(teams.assign_sides(["ZERG REBORN (2-1)", "34-yrs-old men"], sides),
                         ["ZERG REBORN", "34-yrs-old men"])
        # joke outcome name: the other outcome decides
        self.assertEqual(teams.assign_sides(["WHAT IN A BEAKER???", "Seker Matar"],
                                            ["Seker Matar", "Seriously Suspicious"]),
                         ["Seriously Suspicious", "Seker Matar"])
        self.assertIsNone(teams.parse_sides("ATXX Finals Match 1 Outcome"))

    def test_same_team_spellings(self):
        self.assertTrue(teams.same_team("Barcode", "BARCODE"))
        self.assertTrue(teams.same_team("Yabbiy's Baddies", "Yabbiy’s Baddies"))
        self.assertTrue(teams.same_team("Spooky Action at a Distan", "Spooky Action at a Dist."))
        self.assertFalse(teams.same_team("V0LTA", "V0LTB"))
        self.assertFalse(teams.same_team("No Local", "No Numbers"))  # short prefix isn't enough


class OverrideTest(unittest.TestCase):
    def test_name_override_merges_teams(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        overrides = {"usecodewizard": "Catlas Tor UseCodeWIZARD", "catlas tor": "Catlas Tor UseCodeWIZARD",
                     "loop a": "Loop B", "loop b": "Loop A"}
        with mock.patch.object(teams, "lookup_alliance", return_value=None):
            names = {a: teams.ensure_team(conn, a, overrides)
                     for a in ("UseCodeWIZARD", "Catlas Tor", "CatlasTor UseCodeWIZARD", "Loop A")}
        self.assertEqual(set(list(names.values())[:3]), {"Catlas Tor UseCodeWIZARD"})
        self.assertIn(names["Loop A"], ("Loop A", "Loop B"))  # a loop falls back, doesn't hang

    def test_override_target_wins_over_more_used_spelling(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        with mock.patch.object(teams, "lookup_alliance", return_value=None):
            self.assertEqual(teams.ensure_team(conn, "xqty", {}), "xqty")  # seen first, no overrides
            overrides = {"xqties": "XQTY"}
            got = {a: teams.ensure_team(conn, a, overrides, force=True)
                   for a in ("xqty", "XQTY", "xqty[...]", "xqties")}
        self.assertEqual(set(got.values()), {"XQTY"})

    def test_group_takes_the_name_of_its_alliance_with_a_logo(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        bars = "I" * 25
        overrides = {bars.lower(): "Barcode"}
        lookup = lambda *names: (99012243, bars) if any(n.lower() == bars.lower() for n in names) else None
        with mock.patch.object(teams, "lookup_alliance", side_effect=lookup), \
                mock.patch.object(teams, "alliance_logo", return_value=(b"logo", False)), \
                mock.patch.object(teams, "evewho_name", return_value=bars):
            got = {a: teams.ensure_team(conn, a, overrides) for a in ("Barcode", "BARCODE", bars)}
        self.assertEqual(set(got.values()), {bars})
        team = db.get_team(conn, bars)
        self.assertEqual((team["eve_id"], team["logo_is_default"]), (99012243, 0))

    def test_lookup_prefers_alliance_with_its_own_logo(self):
        esi = json.dumps({"alliances": [{"id": 1, "name": "Plain"}, {"id": 2, "name": "Fancy"}]}).encode()
        logos = {1: (teams.DEFAULT_LOGO, True), 2: (b"art", False)}
        with mock.patch.object(teams, "_http", return_value=esi), \
                mock.patch.object(teams, "alliance_logo", side_effect=logos.get):
            self.assertEqual(teams.lookup_alliance("Plain", "Fancy"), (2, "Fancy"))


if __name__ == "__main__":
    unittest.main()
