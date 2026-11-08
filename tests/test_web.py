import json
import re
import tempfile
import unittest
from pathlib import Path

from pointsranker import db, rankings, web
from test_rankings import pred


class SortableTableTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        for team in ("A", "B", "C"):
            db.save_team(self.conn, team, None, None, None, b"", True)
        for p in (pred("m1", "2026-09-13T21:00:00Z", ("A", 300), ("B", 100), winner=0),
                  pred("m2", "2026-09-13T22:00:00Z", ("C", 50), ("B", 400), winner=0),
                  pred("m3", "2026-09-13T23:00:00Z", ("A", 505), ("C", 495), winner=1)):
            db.upsert_prediction(self.conn, "chan", p, {o["id"]: o["title"] for o in p["outcomes"]},
                                 snapshot=False)

    def page(self, view):
        return web.render(self.conn, f"/t/all/{view}/", {}).body.decode()

    def rows(self, html):
        return re.findall(r"<tr (data-team[^>]*)>", html)

    def test_rows_carry_every_metric_in_default_order(self):
        matches = rankings.load_matches(self.conn)
        for view in web.METRICS:
            scoped = [m for m in matches if m.tossup] if view == "tossups" else matches
            stats = rankings.team_stats(scoped, matches)
            html = self.page(view)
            spec = json.loads(re.search(r'id="metrics">(.*?)</script>', html).group(1))
            self.assertEqual([m["key"] for m in spec if m["sort"]], [m.key for m in web.METRICS[view]] + ["team"])
            self.assertEqual({m["key"] for m in spec}, {m.key for m in web.ALL_METRICS} | {"team"})
            rows = self.rows(html)
            order = [re.search(r'data-team="([^"]*)"', r).group(1) for r in rows]
            self.assertEqual(order, [s.team.lower() for s in rankings.ranked(stats, view)], view)
            for r in rows:
                for m in spec:
                    self.assertIn(f'data-{m["key"]}="', r, f"{view} row missing {m['key']}")
            self.assertIn(f'data-default="{web.DEFAULT_SORT[view]}"', html)

    def test_na_metric_is_empty(self):
        rows = self.rows(self.page("won"))
        b = next(r for r in rows if 'data-team="b"' in r)  # B never won
        self.assertIn('data-won_avg=""', b)
        self.assertIn('data-points_won="0"', b)

    def test_tossups_page(self):
        html = self.page("tossups")
        self.assertIn("<h2>Tossup matches</h2>", html)
        self.assertIn('<b>1</b><span>tossups</span>', html)
        self.assertIn('<b>33%</b><span>of all matches</span>', html)
        self.assertIn('<b>0%</b><span>won by the bigger pool</span>', html)  # A led, C won
        self.assertIn("1.0 pts", html)
        self.assertEqual(len(self.rows(html)), 2)    # only A and C played a tossup
        self.assertEqual(html.count('class="sortable"'), 1)

    def test_tossup_count_is_labelled_only_on_tossups_page(self):
        def label(view):
            spec = json.loads(re.search(r'id="metrics">(.*?)</script>', self.page(view)).group(1))
            return next(m["label"] for m in spec if m["key"] == "matches")
        self.assertEqual(label("tossups"), "Tossups")
        for view in ("perceived", "won", "underrated"):
            self.assertEqual(label(view), "Matches", view)

    def test_tossups_page_empty(self):
        self.conn.execute("DELETE FROM outcomes WHERE prediction_id = 'm3'")
        self.conn.execute("DELETE FROM predictions WHERE id = 'm3'")
        html = self.page("tossups")
        self.assertIn("No tossups here.", html)
        self.assertNotIn('id="metrics"', html)

    def test_matches_page_badges_and_upset_rate(self):
        html = self.page("matches")
        self.assertEqual(html.count("<span class=badge>tossup</span>"), 1)
        self.assertEqual(html.count("<span class=badge>upset</span>"), 1)   # m2 only
        self.assertIn('<b>50%</b><span>upsets (crowd got it wrong)</span>', html)  # 1 of 2, tossup excluded
        self.assertIn('<b>1</b><span>tossups</span>', html)


class LinkPreviewTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.odd = 'Quote "Team" <b>'
        for team in (self.odd, "B"):
            db.save_team(self.conn, team, None, None, None, b"", True)
        p = pred("m1", "2026-09-13T21:00:00Z", (self.odd, 300), ("B", 100), winner=0)
        db.upsert_prediction(self.conn, "chan", p, {o["id"]: o["title"] for o in p["outcomes"]}, snapshot=False)

    def meta(self, path):
        html = web.render(self.conn, path, {}, "https://example.com/").body.decode()
        head = html.split("</head>")[0]
        self.assertNotIn("<b>", head)
        return {k: v for k, v in re.findall(r'<meta (?:property|name)="([^"]+)" content="([^"]*)">', head)}

    def test_preview_names_leader_and_uses_absolute_urls(self):
        for path in ("/t/all/perceived/", "/t/all/matches/"):
            m = self.meta(path)
            self.assertEqual(m["og:url"], "https://example.com" + path)
            self.assertIn("&quot;Team&quot; &lt;b&gt;", m["og:description"])
            self.assertEqual(m["og:image"], f"https://example.com/logo/{web.logo_slug(self.odd)}.png")
        self.assertEqual(self.meta("/t/all/bet/")["og:title"], "All time · Points bet")


class BasePathTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        db.save_team(self.conn, "A", None, None, None, b"", True)
        db.save_team(self.conn, "B", None, None, None, b"", True)
        p = pred("m1", "2026-09-13T21:00:00Z", ("A", 300), ("B", 100), winner=0)
        db.upsert_prediction(self.conn, "chan", p, {o["id"]: o["title"] for o in p["outcomes"]}, snapshot=False)

    def tearDown(self):
        self.conn.close()

    def test_default_site_has_root_links(self):
        html = web.render(self.conn, "/t/all/perceived/", {}).body.decode()
        self.assertIn('<a href="/">', html)
        self.assertIn('src="/logo/', html)
        self.assertIn('a[href^="/t/"]', html)

    def test_base_url_path_prefixes_links_and_preview_urls(self):
        html = web.render(self.conn, "/t/all/perceived/", {}, "http://host/tournament-bets").body.decode()
        self.assertIn('<a href="/tournament-bets/">', html)
        self.assertIn('src="/tournament-bets/logo/', html)
        self.assertIn('value="/tournament-bets/t/all/perceived/"', html)
        self.assertIn('a[href^="/tournament-bets/t/"]', html)
        self.assertNotRegex(html, r'(?:href|src|value)="/(?!tournament-bets/)')
        self.assertIn('content="http://host/tournament-bets/t/all/perceived/"', html)

    def test_not_found_page_prefix(self):
        self.assertIn(b'href="/"', web.not_found_page())
        self.assertIn(b'href="/tournament-bets/"', web.not_found_page("http://host/tournament-bets"))


if __name__ == "__main__":
    unittest.main()
