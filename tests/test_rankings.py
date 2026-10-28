import tempfile
import unittest
from pathlib import Path

from pointsranker import db, rankings


def pred(pid, created, a, b, winner):
    """a/b = (team, points); winner = 0, 1 or None."""
    outcomes = [{"id": f"{pid}-{i}", "title": t, "color": "BLUE", "totalPoints": pts, "totalUsers": 1}
                for i, (t, pts) in enumerate((a, b))]
    return {"id": pid, "title": f"{a[0]} vs {b[0]}", "status": "RESOLVED", "createdAt": created,
            "outcomes": outcomes,
            "winningOutcome": {"id": outcomes[winner]["id"]} if winner is not None else None}


class RankingsTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        for team in ("A", "B", "C"):
            db.save_team(self.conn, team, None, None, None, b"", True)

    def add(self, p):
        names = {o["id"]: o["title"] for o in p["outcomes"]}
        db.upsert_prediction(self.conn, "chan", p, names, snapshot=False)

    def test_perceived_winner_and_payouts(self):
        # A is the crowd favourite but loses.
        self.add(pred("m1", "2026-09-13T21:00:00Z", ("A", 1_795_863), ("B", 809_632), winner=1))
        stats = rankings.team_stats(rankings.load_matches(self.conn))
        a, b = stats["A"], stats["B"]
        self.assertEqual((a.p_wins, a.p_losses, b.p_wins, b.p_losses), (1, 0, 0, 1))
        self.assertEqual((a.wins, a.losses, b.wins, b.losses), (0, 1, 1, 0))
        self.assertEqual(b.points_won, 1_795_863)   # B's backers took A's pool
        self.assertEqual(a.points_lost, 1_795_863)  # A's backers lost their pool
        self.assertEqual((a.points_won, b.points_lost), (0, 0))
        self.assertEqual(a.points_bet, 1_795_863)
        self.assertAlmostEqual(a.elo + b.elo, 2 * rankings.ELO_START)
        self.assertGreater(a.elo, b.elo)

    def test_tie_is_no_perceived_result(self):
        self.add(pred("m1", "2026-09-13T21:00:00Z", ("A", 100), ("B", 100), winner=0))
        stats = rankings.team_stats(rankings.load_matches(self.conn))
        self.assertEqual(stats["A"].p_wins + stats["A"].p_losses, 0)
        self.assertAlmostEqual(stats["A"].elo, rankings.ELO_START)

    def test_tournament_split_on_gap(self):
        self.add(pred("m1", "2025-10-24T12:00:00Z", ("A", 1), ("B", 2), 0))
        self.add(pred("m2", "2025-10-26T12:00:00Z", ("A", 1), ("C", 2), 0))
        self.add(pred("m3", "2026-08-29T12:00:00Z", ("B", 1), ("C", 2), 0))
        tours = rankings.tournaments(rankings.load_matches(self.conn), {"2026-08-29": "AT XXII"})
        self.assertEqual([(t.id, len(t.matches)) for t in tours], [("2026-08-29", 1), ("2025-10-24", 2)])
        self.assertEqual(tours[0].label, "AT XXII")
        self.assertEqual(tours[1].label, "Oct 24 – Oct 26 2025")

    def test_feeders_and_finals_form_one_tournament(self):
        self.add(pred("f1", "2025-08-30T12:00:00Z", ("A", 1), ("B", 2), 0))
        self.add(pred("f2", "2025-09-07T12:00:00Z", ("A", 1), ("C", 2), 0))
        self.add(pred("x1", "2025-10-24T12:00:00Z", ("B", 1), ("C", 2), 0))  # 47 days later
        self.add(pred("y1", "2026-04-11T12:00:00Z", ("A", 1), ("C", 2), 0))  # unrelated event
        self.add(pred("y2", "2026-06-01T12:00:00Z", ("B", 1), ("C", 2), 0))  # but forced apart
        names = {"2025-08-30": {"name": "ATXXI", "stage": "Feeders"}, "2026-06-01": {"new_tournament": True}}
        tours = rankings.tournaments(rankings.load_matches(self.conn), names)
        self.assertEqual([t.id for t in tours], ["2026-06-01", "2026-04-11", "2025-08-30"])
        at = tours[-1]
        self.assertEqual(at.label, "ATXXI")
        self.assertEqual([(s.id, s.label, len(s.matches)) for s in at.stages],
                         [("2025-08-30-feeders", "Feeders", 2), ("2025-08-30-finals", "Finals", 1)])
        self.assertEqual(len(at.matches), 3)
        self.assertEqual([s.label for s in tours[0].stages], [None])

    def test_ranked_views(self):
        self.add(pred("m1", "2026-09-13T21:00:00Z", ("A", 300), ("B", 100), winner=0))
        self.add(pred("m2", "2026-09-13T22:00:00Z", ("C", 50), ("B", 400), winner=0))
        stats = rankings.team_stats(rankings.load_matches(self.conn))
        self.assertEqual([s.team for s in rankings.ranked(stats, "bet")], ["B", "A", "C"])
        self.assertEqual(rankings.ranked(stats, "won")[0].team, "C")  # took B's 400
        self.assertEqual(rankings.ranked(stats, "lost")[0].team, "B")  # lost 100 + 400


if __name__ == "__main__":
    unittest.main()
