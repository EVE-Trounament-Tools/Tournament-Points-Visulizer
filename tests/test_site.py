import re
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from pointsranker import db, site
from test_rankings import pred


def make_db(tmp: Path) -> Path:
    conn = db.connect(tmp / "t.db")
    for team in ("Alpha.", "Beta Team"):
        db.save_team(conn, team, None, None, None, b"png", True)
    for pid, created in (("m1", "2025-10-24T12:00:00Z"), ("m2", "2026-08-29T12:00:00Z"),
                         ("m3", "2026-10-20T12:00:00Z")):
        p = pred(pid, created, ("Alpha.", 300), ("Beta Team", 100), 1)
        db.upsert_prediction(conn, "chan", p, {o["id"]: o["title"] for o in p["outcomes"]}, snapshot=False)
    conn.close()
    return tmp / "t.db"


class BuildTest(unittest.TestCase):
    def test_build_has_no_broken_links(self):
        tmp = Path(tempfile.mkdtemp())
        make_db(tmp)

        out = tmp / "site"
        site.build(tmp / "t.db", out, {"2026-08-29": "AT XXII"})
        for f in ("index.html", "404.html", "t/all/perceived/index.html", "t/all/tossups/index.html",
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


    def test_build_below_domain_root_prefixes_every_link(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "site"
        site.build(make_db(tmp), out, {"2026-08-29": "AT XXII"}, "http://host/tournament-bets/")
        pages = list(out.rglob("*.html"))
        self.assertIn(out / "404.html", pages)
        for page in pages:
            html = page.read_text()
            refs = re.findall(r'(?:href|src|value)="(/[^"]*)"', html)
            self.assertTrue(refs, page)
            for ref in refs:
                self.assertTrue(ref.startswith("/tournament-bets/"), f"{page.relative_to(out)}: {ref}")
                target = out / ref[len("/tournament-bets/"):]
                self.assertTrue(target.is_file() or (target / "index.html").is_file(),
                                f"{page.relative_to(out)} links to missing {ref}")
            self.assertNotIn('a[href^="/t/"]', html)


class PublisherTest(unittest.TestCase):
    def test_rebuild_only_builds_without_deploying(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "site"
        with mock.patch.object(site, "PUBLISH_INTERVAL", 0), \
             mock.patch.object(site, "deploy") as deploy:
            pub = site.Publisher(make_db(tmp), out, {}, deploy=False)
            pub.request()
            for _ in range(100):
                if (out / "index.html").exists():
                    break
                time.sleep(0.05)
            pub.stop()
        self.assertTrue((out / "index.html").is_file())
        deploy.assert_not_called()

    def test_publish_deploys(self):
        tmp = Path(tempfile.mkdtemp())
        done = threading.Event()
        with mock.patch.object(site, "PUBLISH_INTERVAL", 0), \
             mock.patch.object(site, "deploy", side_effect=lambda out: done.set()) as deploy:
            pub = site.Publisher(make_db(tmp), tmp / "site", {})
            pub.request()
            self.assertTrue(done.wait(5))
            pub.stop()
        deploy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
