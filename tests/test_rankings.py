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

    def test_tossup_boundary(self):
        self.add(pred("close", "2026-09-13T21:00:00Z", ("A", 520), ("B", 480), winner=1))
        self.add(pred("edge", "2026-09-13T22:00:00Z", ("A", 525), ("B", 475), winner=1))
        close, edge = rankings.load_matches(self.conn)
        self.assertTrue(close.tossup)
        self.assertIsNone(close.perceived_winner)
        self.assertEqual(close.pool_leader, "A")
        self.assertFalse(close.upset)                # the bigger pool lost, but it's a tossup
        self.assertFalse(edge.tossup)                # exactly 5 points apart is a real call
        self.assertEqual(edge.perceived_winner, "A")
        self.assertTrue(edge.upset)

    def test_tossup_excluded_from_crowd_stats_but_not_real_results(self):
        self.add(pred("m1", "2026-09-13T21:00:00Z", ("A", 300), ("B", 100), winner=0))
        self.add(pred("m2", "2026-09-13T22:00:00Z", ("A", 510), ("B", 490), winner=1))
        stats = rankings.team_stats(rankings.load_matches(self.conn))
        a, b = stats["A"], stats["B"]
        self.assertEqual((a.p_wins, a.p_losses, b.p_wins, b.p_losses), (1, 0, 0, 1))
        self.assertEqual(b.upset_wins, 0)
        self.assertEqual((a.wins, a.losses, b.wins, b.losses), (1, 1, 1, 1))  # real results count
        self.assertEqual((b.points_won, a.points_lost), (510, 510))
        self.assertEqual((a.bets, b.bets), (2, 2))
        self.assertAlmostEqual(a.win_pct, 0.5)
        self.assertEqual(a.called_win_pct, 1.0)      # the tossup loss doesn't make A "overrated"
        self.assertEqual(a.underrated, 0.0)

    def test_tossup_is_a_draw_for_elo(self):
        self.add(pred("m1", "2026-09-13T21:00:00Z", ("A", 300), ("B", 100), winner=0))
        self.add(pred("m2", "2026-09-13T22:00:00Z", ("A", 510), ("B", 490), winner=0))
        matches = rankings.load_matches(self.conn)
        before, after = rankings.elo_ratings(matches[:1]), rankings.elo_ratings(matches)
        self.assertLess(after["A"], before["A"])     # the higher-rated team drew
        self.assertGreater(after["A"], rankings.ELO_START)

    def test_elo_is_all_time(self):
        self.add(pred("m1", "2025-10-24T12:00:00Z", ("A", 300), ("B", 100), 0))  # earlier tournament
        self.add(pred("m2", "2026-08-29T12:00:00Z", ("A", 100), ("C", 300), 1))
        history = rankings.load_matches(self.conn)
        latest = rankings.tournaments(history)[0].matches
        everything = rankings.team_stats(history)
        scoped = rankings.team_stats(latest, history)
        self.assertNotIn("B", scoped)
        self.assertEqual(scoped["A"].elo, everything["A"].elo)  # A's earlier win still counts
        self.assertNotAlmostEqual(scoped["A"].elo, rankings.team_stats(latest)["A"].elo)

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

        info = rankings.match_info(tours)
        self.assertEqual([(info[m].number, info[m].phase) for m in ("f1", "f2", "x1")],
                         [(1, "Prelim"), (2, "Prelim"), (3, "Finals")])
        self.assertEqual((info["y1"].number, info["y1"].phase), (1, None))

    def test_loyal_bettor_rewards_upsets_and_penalises_losses(self):
        # A wins as a 90% favourite, then C (10% underdog) upsets B, then A loses.
        self.add(pred("m1", "2026-09-13T21:00:00Z", ("A", 900_000), ("B", 100_000), winner=0))
        self.add(pred("m2", "2026-09-13T22:00:00Z", ("C", 100_000), ("B", 900_000), winner=0))
        self.add(pred("m3", "2026-09-13T23:00:00Z", ("A", 500_000), ("C", 500_000), winner=1))
        stats = rankings.team_stats(rankings.load_matches(self.conn))
        a, c = stats["A"], stats["C"]
        fav_win = rankings.bet_return(900_000, 1_000_000, True)
        dog_win = rankings.bet_return(100_000, 1_000_000, True)
        self.assertAlmostEqual(fav_win / rankings.STAKE, 0.111, places=3)
        self.assertAlmostEqual(dog_win / rankings.STAKE, 8.91, places=2)
        self.assertEqual(rankings.bet_return(500_000, 1_000_000, False), -rankings.STAKE)
        self.assertEqual((a.bets, c.bets, a.upset_wins, c.upset_wins), (2, 2, 0, 1))
        self.assertAlmostEqual(a.bet_profit, fav_win - rankings.STAKE)  # the loss outweighs the safe win
        self.assertAlmostEqual(c.best_win, dog_win)
        self.assertAlmostEqual(c.roi, (dog_win + rankings.bet_return(500_000, 1_000_000, True))
                               / (2 * rankings.STAKE))
        self.assertEqual([s.team for s in rankings.ranked(stats, "loyal")], ["C", "A", "B"])
        self.assertEqual(rankings.bet_return(0, 50_000, True), 50_000)  # nobody else backed it

    def test_ranked_views(self):
        self.add(pred("m1", "2026-09-13T21:00:00Z", ("A", 300), ("B", 100), winner=0))
        self.add(pred("m2", "2026-09-13T22:00:00Z", ("C", 50), ("B", 400), winner=0))
        stats = rankings.team_stats(rankings.load_matches(self.conn))
        self.assertEqual([s.team for s in rankings.ranked(stats, "bet")], ["B", "A", "C"])
        self.assertEqual(rankings.ranked(stats, "won")[0].team, "C")  # took B's 400
        self.assertEqual(rankings.ranked(stats, "lost")[0].team, "B")  # lost 100 + 400


if __name__ == "__main__":
    unittest.main()
