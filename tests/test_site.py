import re
import tempfile
import unittest
from pathlib import Path

from pointsranker import db, site
from test_rankings import pred


class BuildTest(unittest.TestCase):
    def test_build_has_no_broken_links(self):
        tmp = Path(tempfile.mkdtemp())
        conn = db.connect(tmp / "t.db")
        for team in ("Alpha.", "Beta Team"):
            db.save_team(conn, team, None, None, None, b"png", True)
        for pid, created in (("m1", "2025-10-24T12:00:00Z"), ("m2", "2026-08-29T12:00:00Z"),
                             ("m3", "2026-10-20T12:00:00Z")):
            p = pred(pid, created, ("Alpha.", 300), ("Beta Team", 100), 1)
            db.upsert_prediction(conn, "chan", p, {o["id"]: o["title"] for o in p["outcomes"]}, snapshot=False)
        conn.close()

        out = tmp / "site"
        site.build(tmp / "t.db", out, {"2026-08-29": "AT XXII"})
        for f in ("index.html", "404.html", "t/all/perceived/index.html",
                  "t/2026-08-29/matches/index.html", "t/2025-10-24/lost/index.html",
                  "t/2026-08-29-finals/won/index.html", "t/2026-08-29-prelims/bet/index.html"):
            self.assertTrue((out / f).is_file(), f)
        self.assertEqual(len(list((out / "logo").glob("*.png"))), 2)

        for page in out.rglob("*.html"):
            for ref in re.findall(r'(?:href|src|value)="(/[^"]*)"', page.read_text()):
                target = out / ref.lstrip("/")
                self.assertTrue(target.is_file() or (target / "index.html").is_file(),
                                f"{page.relative_to(out)} links to missing {ref}")

        site.build(tmp / "t.db", out, {})  # rebuilding replaces the old tree
        self.assertFalse(any(tmp.glob(".site-*")) or (tmp / "site.old").exists())


if __name__ == "__main__":
    unittest.main()
