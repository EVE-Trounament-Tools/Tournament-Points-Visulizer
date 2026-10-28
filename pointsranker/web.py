"""Local rankings website: stdlib HTTP server rendering pages straight from the DB."""
import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import rankings

log = logging.getLogger(__name__)

VIEW_LABELS = {
    "perceived": "Perceived",
    "bet": "Points bet",
    "won": "Points won",
    "lost": "Points lost",
}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#14171c;--muted:#5f6670;--line:#e3e6ea;--accent:#2f6fde;
--accent-bg:#e8f0fd;--win:#1d8a4e;--loss:#c23b3b;--chip:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--fg:#e8eaed;--muted:#9aa1ab;
--line:#2a2f38;--accent:#6ea0ff;--accent-bg:#1c2740;--win:#4cc584;--loss:#ff7a7a;--chip:#222731}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:end;justify-content:space-between;margin-bottom:18px}
h1{font-size:22px;margin:0}h1 a{color:inherit;text-decoration:none}
.tours{margin:0 0 12px}.tours ul{margin:6px 0;padding-left:18px}.tours a{color:var(--accent)}
.sub{color:var(--muted);font-size:13px}
header select{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
nav{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
nav a{padding:6px 12px;border-radius:999px;background:var(--chip);color:var(--fg);text-decoration:none;font-size:14px}
nav a.on{background:var(--accent);color:var(--card)}
.summary{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px}
.stat b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:12px}
.note{color:var(--muted);font-size:13px;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 12px;text-align:left;white-space:nowrap}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);border-bottom:1px solid var(--line)}
tr+tr td{border-top:1px solid var(--line)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
td.rank{color:var(--muted);width:1%}
.team{display:flex;align-items:center;gap:10px}
.team img{width:32px;height:32px;border-radius:6px;flex:none}
.fav{font-weight:600}
.w{color:var(--win)}.l{color:var(--loss)}
.badge{font-size:11px;padding:2px 7px;border-radius:999px;background:var(--accent-bg);color:var(--accent);font-weight:600}
.empty{padding:24px;color:var(--muted)}
"""


def _fmt(n: float) -> str:
    return f"{n:,.0f}"


def logo_slug(name: str) -> str:
    """Filesystem- and URL-safe logo file name, unique per team name."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "team"
    return f"{base}-{hashlib.md5(name.encode()).hexdigest()[:6]}"


def page_url(tournament: str, view: str) -> str:
    return f"/t/{tournament}/{view}/"


def _team(name: str, bold: bool = False) -> str:
    cls = ' class="fav"' if bold else ""
    return (f'<span class="team"><img src="/logo/{logo_slug(name)}.png" alt="" loading="lazy">'
            f'<span{cls}>{escape(name)}</span></span>')


def _summary(matches: list[rankings.Match]) -> str:
    total = sum(m.total for m in matches)
    upsets = sum(m.upset for m in matches)
    decided = sum(1 for m in matches if m.winner and m.perceived_winner)
    items = [(_fmt(len(matches)), "matches"), (_fmt(total), "points wagered"),
             (f"{upsets / decided:.0%}" if decided else "–", "upsets (crowd got it wrong)")]
    return '<div class="summary">' + "".join(
        f'<div class="stat"><b>{v}</b><span>{k}</span></div>' for v, k in items) + "</div>"


def _ranking_table(stats: dict[str, rankings.Stats], view: str) -> str:
    cols = {
        "perceived": [("Perceived W-L", lambda s: f"{s.p_wins}-{s.p_losses}"),
                      ("Win %", lambda s: f"{s.p_win_pct:.0%}"),
                      ("Elo", lambda s: _fmt(s.elo)),
                      ("Avg share", lambda s: f"{s.avg_share:.0%}"),
                      ("Actual W-L", lambda s: f"{s.wins}-{s.losses}")],
        "bet": [("Points bet", lambda s: _fmt(s.points_bet)),
                ("Bettors", lambda s: _fmt(s.users_bet)),
                ("Matches", lambda s: s.matches),
                ("Avg / match", lambda s: _fmt(s.points_bet / s.matches))],
        "won": [("Points won", lambda s: _fmt(s.points_won)),
                ("Actual wins", lambda s: s.wins),
                ("Avg / win", lambda s: _fmt(s.points_won / s.wins) if s.wins else "–")],
        "lost": [("Points lost", lambda s: _fmt(s.points_lost)),
                 ("Actual losses", lambda s: s.losses),
                 ("Avg / loss", lambda s: _fmt(s.points_lost / s.losses) if s.losses else "–")],
    }[view]
    head = '<tr><th class="n">#</th><th>Team</th>' + "".join(f'<th class="n">{c}</th>' for c, _ in cols) + "</tr>"
    rows = []
    for i, s in enumerate(rankings.ranked(stats, view), 1):
        rows.append(f'<tr><td class="n rank">{i}</td><td>{_team(s.team)}</td>'
                    + "".join(f'<td class="n">{f(s)}</td>' for _, f in cols) + "</tr>")
    return f'<div class="card"><table>{head}{"".join(rows)}</table></div>'


def _matches_table(matches: list[rankings.Match]) -> str:
    head = ('<tr><th>Date</th><th>Team</th><th class="n">Points</th><th class="n">Points</th>'
            '<th>Team</th><th>Winner</th><th></th></tr>')
    rows = []
    for m in reversed(matches):
        fav = m.perceived_winner
        rows.append(
            f'<tr><td>{escape(m.created_at[:10])}</td>'
            f'<td>{_team(m.a.team, fav == m.a.team)}</td><td class="n">{_fmt(m.a.points)}</td>'
            f'<td class="n">{_fmt(m.b.points)}</td><td>{_team(m.b.team, fav == m.b.team)}</td>'
            f'<td>{escape(m.winner or "–")}</td>'
            f'<td>{"<span class=badge>upset</span>" if m.upset else ""}</td></tr>')
    return f'<div class="card"><table>{head}{"".join(rows)}</table></div>'


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    cache: str = "no-store"


PAGE_RE = re.compile(r"^/t/([\w-]+)/(\w+)/?$")
VIEWS = [*VIEW_LABELS, "matches"]
NOTES = {
    "perceived": "A team gets a perceived win when chat bet more channel points on it than on its "
                 "opponent, whatever the real result. Elo runs over those perceived results in match order.",
    "bet": "Total channel points bet on the team across its matches.",
    "won": "Points the team's backers took from the opposing pool when the team actually won.",
    "lost": "Points the team's backers lost when the team actually lost.",
    "matches": "Bold team = crowd favourite (more points). Upset = favourite lost.",
}


def _page(tours: list[rankings.Tournament], all_matches: list[rankings.Match], t: str, view: str) -> str:
    tour = next((x for x in tours if x.id == t), None)
    scoped = tour.matches if tour else all_matches
    options = [("all", "All time")] + [(x.id, f"{x.label} · {len(x.matches)} matches") for x in tours]
    select = "".join(f'<option value="{page_url(v, view)}"{" selected" if v == t else ""}>{escape(label)}</option>'
                     for v, label in options)
    links = "".join(f'<li><a href="{page_url(v, view)}">{escape(label)}</a></li>' for v, label in options)
    tabs = "".join(f'<a href="{page_url(t, v)}"{" class=on" if v == view else ""}>{label}</a>'
                   for v, label in [*VIEW_LABELS.items(), ("matches", "Matches")])
    if not scoped:
        body = '<div class="card empty">No matches recorded yet.</div>'
    elif view == "matches":
        body = _matches_table(scoped)
    else:
        body = _ranking_table(rankings.team_stats(scoped), view)
    title = tour.label if tour else "All time"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pointsRanker · {escape(title)}</title><style>{CSS}</style></head><body><main>
<header><div><h1><a href="/">pointsRanker</a></h1><div class="sub">Team rankings from Twitch channel-point predictions</div></div>
<select onchange="location=this.value" aria-label="Tournament">{select}</select></header>
<noscript><details class="tours"><summary>Tournaments</summary><ul>{links}</ul></details></noscript>
<nav>{tabs}</nav>{_summary(scoped)}<p class="note">{NOTES[view]}</p>{body}
</main></body></html>"""


def page_paths(conn: sqlite3.Connection, names: dict[str, str]) -> list[str]:
    """Every page path the site has, for the static build."""
    tours = rankings.tournaments(rankings.load_matches(conn), names)
    return ["/"] + [page_url(t, v) for t in ["all", *(x.id for x in tours)] for v in VIEWS]


def logo_paths(conn: sqlite3.Connection) -> list[str]:
    return [f"/logo/{logo_slug(r['name'])}.png" for r in conn.execute("SELECT name FROM teams")]


def render(conn: sqlite3.Connection, path: str, names: dict[str, str]) -> Response | None:
    """Render one site path, or None if there's no such page."""
    if path.startswith("/logo/") and path.endswith(".png"):
        slug = path[len("/logo/"):-len(".png")]
        for r in conn.execute("SELECT name, logo FROM teams"):
            if logo_slug(r["name"]) == slug:
                return Response(200, "image/png", r["logo"], "public, max-age=86400")
        return None
    matches = rankings.load_matches(conn)
    tours = rankings.tournaments(matches, names)
    if path == "/":
        t, view = (tours[0].id if tours else "all"), "perceived"
    elif m := PAGE_RE.match(path):
        t, view = m.groups()
        if view not in VIEWS or (t != "all" and all(x.id != t for x in tours)):
            return None
    else:
        return None
    return Response(200, "text/html; charset=utf-8", _page(tours, matches, t, view).encode())


def not_found_page() -> bytes:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>pointsRanker · Not found</title>'
            f'<style>{CSS}</style></head><body><main><h1>Not found</h1>'
            f'<p class="note">That page doesn\'t exist. <a href="/">Back to the rankings</a>.</p>'
            f'</main></body></html>').encode()


def make_handler(db_path: Path, names: dict[str, str]):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, r: Response) -> None:
            self.send_response(r.status)
            self.send_header("Content-Type", r.content_type)
            self.send_header("Content-Length", str(len(r.body)))
            self.send_header("Cache-Control", r.cache)
            self.end_headers()
            self.wfile.write(r.body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/matches") and query:  # old ?t=&view= links
                view = "matches" if url.path == "/matches" else query.get("view", "perceived")
                self.send_response(301)
                self.send_header("Location", page_url(query.get("t", "all"), view))
                self.end_headers()
                return
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                try:
                    r = render(conn, url.path, names)
                finally:
                    conn.close()
            except sqlite3.Error as e:
                log.exception("request failed")
                r = Response(500, "text/plain", f"database error: {e}".encode())
            self._send(r or Response(404, "text/html; charset=utf-8", not_found_page()))

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

    return Handler


def serve(db_path: Path, host: str, port: int, names: dict[str, str]) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(db_path.resolve(), names))
    log.info("serving rankings on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
