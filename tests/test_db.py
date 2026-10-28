import sqlite3
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
