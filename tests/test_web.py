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
                  pred("m2", "2026-09-13T22:00:00Z", ("C", 50), ("B", 400), winner=0)):
            db.upsert_prediction(self.conn, "chan", p, {o["id"]: o["title"] for o in p["outcomes"]},
                                 snapshot=False)

    def page(self, view):
        return web.render(self.conn, f"/t/all/{view}/", {}).body.decode()

    def rows(self, html):
        return re.findall(r"<tr (data-team[^>]*)>", html)

    def test_rows_carry_every_metric_in_default_order(self):
        stats = rankings.team_stats(rankings.load_matches(self.conn))
        for view in web.METRICS:
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


if __name__ == "__main__":
    unittest.main()
