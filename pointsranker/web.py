"""Local rankings website: stdlib HTTP server rendering pages straight from the DB."""
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from . import rankings

log = logging.getLogger(__name__)

VIEW_LABELS = {
    "perceived": "Perceived",
    "bet": "Points bet",
    "won": "Points won",
    "lost": "Points lost",
    "loyal": "Loyal bettor",
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
nav.stages{margin-bottom:8px}nav.stages a{font-size:13px;padding:4px 10px;background:transparent;border:1px solid var(--line)}
nav.stages a.on{background:var(--accent-bg);color:var(--accent);border-color:var(--accent)}
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
.sorter{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;margin:0 0 10px;font-size:13px}
.sorter[hidden]{display:none}
.sorter .lbl{color:var(--muted)}
.sorter .lvl{display:inline-flex;align-items:center;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:2px}
.sorter select{font:inherit;border:0;background:var(--card);color:var(--fg);padding:3px 4px;max-width:46vw}
.sorter button{font:inherit;border:0;background:var(--chip);color:var(--fg);border-radius:6px;padding:3px 8px;cursor:pointer;white-space:nowrap}
.sorter button.rm{background:transparent;color:var(--muted);padding:3px 6px}
.sorter button.add{background:transparent;border:1px dashed var(--line);color:var(--accent)}
.sorter button.reset{background:transparent;color:var(--muted);text-decoration:underline}
footer{margin-top:28px;color:var(--muted);font-size:12px}
"""

FOOTER = ("<footer>EVE Online and the EVE logo are the registered trademarks of CCP hf. Alliance logos "
          "are © CCP hf. This site is not affiliated with or endorsed by CCP hf. or Twitch.</footer>")

# Client-side multi-level sorting for ranking tables. The sort lives in the URL hash
# (#sort=elo:d,points_bet:a) so sorted views can be shared, and is carried over to links
# for the same view (other tournaments/stages) but not to views with different metrics.
SORT_JS = r"""(() => {
const table = document.querySelector("table.sortable"), box = document.querySelector(".sorter");
const metrics = JSON.parse(document.getElementById("metrics").textContent);
const byKey = Object.fromEntries(metrics.map(m => [m.key, m]));
const view = table.dataset.view, def = table.dataset.default;
const parse = str => (str || "").split(",").map(x => x.split(":"))
  .filter(([k], i, all) => byKey[k] && all.findIndex(([j]) => j === k) === i)
  .map(([key, d]) => ({key, dir: d === "a" ? "a" : "d"}));
const fromHash = (location.hash.match(/sort=([^&]*)/) || [])[1];
let state = parse(fromHash && decodeURIComponent(fromHash));
if (!state.length) state = parse(def);
const enc = () => state.map(s => s.key + ":" + s.dir).join(",");
const get = (tr, k) => tr.getAttribute("data-" + k);
function cmp(a, b) {
  for (const {key, dir} of state) {
    const x = get(a, key), y = get(b, key);
    let c;
    if (key === "team") c = x.localeCompare(y);
    else if (x === "" || y === "") { if (x !== y) return x === "" ? 1 : -1; continue; }
    else c = parseFloat(x) - parseFloat(y);
    if (c) return dir === "a" ? c : -c;
  }
  return get(a, "team").localeCompare(get(b, "team"));
}
function el(tag, props, text) {
  const e = Object.assign(document.createElement(tag), props);
  if (text !== undefined) e.textContent = text;
  return e;
}
function apply() {
  const body = table.tBodies[0];
  [...body.rows].sort(cmp).forEach((r, i) => { r.cells[0].textContent = i + 1; body.appendChild(r); });
  const hash = enc() === def ? "" : "#sort=" + enc();
  history.replaceState(null, "", location.pathname + location.search + hash);
  const keep = u => (u = u.split("#")[0], u.endsWith("/" + view + "/") ? u + hash : u);
  document.querySelectorAll('a[href^="/t/"]').forEach(a => a.setAttribute("href", keep(a.getAttribute("href"))));
  document.querySelectorAll("header select option").forEach(o => { o.value = keep(o.value); });
  render();
}
function render() {
  box.replaceChildren(el("span", {className: "lbl"}, "Sort by"));
  state.forEach((s, i) => {
    if (i) box.append(el("span", {className: "lbl"}, "then"));
    const lvl = el("span", {className: "lvl"});
    const sel = el("select", {ariaLabel: "Sort level " + (i + 1)});
    for (const m of metrics) {
      const o = new Option(m.label, m.key, false, m.key === s.key);
      o.disabled = m.key !== s.key && state.some(x => x.key === m.key);
      sel.append(o);
    }
    sel.onchange = () => { s.key = sel.value; s.dir = byKey[s.key].dir; apply(); };
    const team = s.key === "team", down = s.dir === "d";
    const dir = el("button", {type: "button", className: "dir",
      title: team ? (down ? "Z to A" : "A to Z") : (down ? "Highest first" : "Lowest first")},
      team ? (down ? "Z→A" : "A→Z") : (down ? "↓ high" : "↑ low"));
    dir.onclick = () => { s.dir = down ? "a" : "d"; apply(); };
    lvl.append(sel, dir);
    if (i) {
      const rm = el("button", {type: "button", className: "rm", title: "Remove", ariaLabel: "Remove sort level"}, "×");
      rm.onclick = () => { state.splice(i, 1); apply(); };
      lvl.append(rm);
    }
    box.append(lvl);
  });
  const free = metrics.find(m => !state.some(s => s.key === m.key));
  if (free) {
    const add = el("button", {type: "button", className: "add"}, "+ Then by");
    add.onclick = () => { state.push({key: free.key, dir: free.dir}); apply(); };
    box.append(add);
  }
  if (enc() !== def) {
    const reset = el("button", {type: "button", className: "reset"}, "Reset");
    reset.onclick = () => { state = parse(def); apply(); };
    box.append(reset);
  }
  box.hidden = false;
}
apply();
})();"""


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


@dataclass
class Metric:
    key: str
    label: str
    value: Callable[[rankings.Stats], float | None]  # sort value; None = n/a
    desc: bool = True                                  # default sort direction


@dataclass
class Column:
    label: str
    show: Callable[[rankings.Stats], str]


def _signed(v: float, text: str) -> str:
    """Coloured +/− figure; `text` is the formatted magnitude."""
    if round(v, 6) == 0:
        return text
    return f'<span class="{"w" if v > 0 else "l"}">{"+" if v > 0 else "−"}{text}</span>'


def _per(total: float, n: int) -> float | None:
    return total / n if n else None


TEAM = Metric("team", "Team", lambda s: None, desc=False)  # sorted by name, see _row_attrs
METRICS: dict[str, list[Metric]] = {
    "perceived": [Metric("p_wins", "Perceived wins", lambda s: s.p_wins),
                  Metric("p_losses", "Perceived losses", lambda s: s.p_losses, desc=False),
                  Metric("p_pct", "Win %", lambda s: s.p_win_pct),
                  Metric("elo", "Elo", lambda s: s.elo),
                  Metric("share", "Avg share", lambda s: s.avg_share),
                  Metric("wins", "Actual wins", lambda s: s.wins),
                  Metric("losses", "Actual losses", lambda s: s.losses, desc=False),
                  Metric("matches", "Matches", lambda s: s.matches)],
    "bet": [Metric("points_bet", "Points bet", lambda s: s.points_bet),
            Metric("bettors", "Bettors", lambda s: s.users_bet),
            Metric("matches", "Matches", lambda s: s.matches),
            Metric("bet_avg", "Avg / match", lambda s: _per(s.points_bet, s.matches))],
    "won": [Metric("points_won", "Points won", lambda s: s.points_won),
            Metric("wins", "Actual wins", lambda s: s.wins),
            Metric("won_avg", "Avg / win", lambda s: _per(s.points_won, s.wins))],
    "lost": [Metric("points_lost", "Points lost", lambda s: s.points_lost),
             Metric("losses", "Actual losses", lambda s: s.losses),
             Metric("lost_avg", "Avg / loss", lambda s: _per(s.points_lost, s.losses))],
    "loyal": [Metric("roi", "ROI", lambda s: s.roi if s.bets else None),
              Metric("profit", "Profit", lambda s: s.bet_profit if s.bets else None),
              Metric("bets", "Bets", lambda s: s.bets),
              Metric("wins", "Actual wins", lambda s: s.wins),
              Metric("losses", "Actual losses", lambda s: s.losses, desc=False),
              Metric("upsets", "Upset wins", lambda s: s.upset_wins),
              Metric("best", "Best win", lambda s: s.best_win if s.wins else None)],
}
COLUMNS: dict[str, list[Column]] = {
    "perceived": [Column("Perceived W-L", lambda s: f"{s.p_wins}-{s.p_losses}"),
                  Column("Win %", lambda s: f"{s.p_win_pct:.0%}"),
                  Column("Elo", lambda s: _fmt(s.elo)),
                  Column("Avg share", lambda s: f"{s.avg_share:.0%}"),
                  Column("Actual W-L", lambda s: f"{s.wins}-{s.losses}")],
    "bet": [Column("Points bet", lambda s: _fmt(s.points_bet)),
            Column("Bettors", lambda s: _fmt(s.users_bet)),
            Column("Matches", lambda s: str(s.matches)),
            Column("Avg / match", lambda s: _fmt(s.points_bet / s.matches))],
    "won": [Column("Points won", lambda s: _fmt(s.points_won)),
            Column("Actual wins", lambda s: str(s.wins)),
            Column("Avg / win", lambda s: _fmt(s.points_won / s.wins) if s.wins else "–")],
    "lost": [Column("Points lost", lambda s: _fmt(s.points_lost)),
             Column("Actual losses", lambda s: str(s.losses)),
             Column("Avg / loss", lambda s: _fmt(s.points_lost / s.losses) if s.losses else "–")],
    "loyal": [Column("Bets", lambda s: str(s.bets)),
              Column("W-L", lambda s: f"{s.wins}-{s.losses}"),
              Column("Upset wins", lambda s: str(s.upset_wins)),
              Column("Profit", lambda s: _signed(s.bet_profit, _fmt(abs(s.bet_profit))) if s.bets else "–"),
              Column("ROI", lambda s: _signed(s.roi, f"{abs(s.roi):.0%}") if s.bets else "–"),
              Column("Best win", lambda s: f"+{_fmt(s.best_win)}" if s.wins else "–")],
}
# Mirrors rankings.VIEWS, which gives the server-side (no-JS) order.
DEFAULT_SORT = {
    "perceived": "p_wins:d,p_pct:d,elo:d",
    "bet": "points_bet:d",
    "won": "points_won:d",
    "lost": "points_lost:d",
    "loyal": "roi:d,profit:d",
}


def _row_attrs(s: rankings.Stats, metrics: list[Metric]) -> str:
    def raw(v):
        return "" if v is None else repr(round(v, 6)) if isinstance(v, float) else str(v)
    attrs = [f'data-team="{escape(s.team.lower())}"']
    attrs += [f'data-{m.key}="{raw(m.value(s))}"' for m in metrics]
    return " ".join(attrs)


def _ranking_table(stats: dict[str, rankings.Stats], view: str) -> str:
    metrics, cols = METRICS[view], COLUMNS[view]
    spec = json.dumps([{"key": m.key, "label": m.label, "dir": "d" if m.desc else "a"}
                       for m in [*metrics, TEAM]])
    head = '<tr><th class="n">#</th><th>Team</th>' + "".join(f'<th class="n">{c.label}</th>' for c in cols) + "</tr>"
    rows = []
    for i, s in enumerate(rankings.ranked(stats, view), 1):
        rows.append(f'<tr {_row_attrs(s, metrics)}><td class="n rank">{i}</td><td>{_team(s.team)}</td>'
                    + "".join(f'<td class="n">{c.show(s)}</td>' for c in cols) + "</tr>")
    return (f'<div class="sorter" hidden></div>'
            f'<script type="application/json" id="metrics">{spec}</script>'
            f'<div class="card"><table class="sortable" data-view="{view}" data-default="{DEFAULT_SORT[view]}">'
            f'<thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<script>{SORT_JS}</script>')


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
    "loyal": f"Profit from betting {rankings.STAKE:,} points on this team in every match it played. "
             f"Wins pay out at the match's real odds (upsets pay big, heavy-favourite wins pay little); "
             f"every loss costs the full {rankings.STAKE:,}.",
    "matches": "Bold team = crowd favourite (more points). Upset = favourite lost.",
}


def _scope(tours: list[rankings.Tournament], scope: str):
    """(tournament, stage) for a scope id: "all", a tournament id or a stage id."""
    for t in tours:
        if t.id == scope:
            return t, None
        for st in t.stages:
            if len(t.stages) > 1 and st.id == scope:
                return t, st
    return None, None


def _tour_option(t: rankings.Tournament) -> str:
    stage = f" · {t.stages[0].label}" if len(t.stages) == 1 and t.stages[0].label else ""
    return f"{t.label}{stage} · {len(t.matches)} matches"


def _page(tours: list[rankings.Tournament], all_matches: list[rankings.Match], scope: str, view: str) -> str:
    tour, stage = _scope(tours, scope)
    scoped = stage.matches if stage else tour.matches if tour else all_matches
    current = tour.id if tour else "all"
    options = [("all", "All time")] + [(x.id, _tour_option(x)) for x in tours]
    select = "".join(f'<option value="{page_url(v, view)}"{" selected" if v == current else ""}>{escape(label)}</option>'
                     for v, label in options)
    links = "".join(f'<li><a href="{page_url(v, view)}">{escape(label)}</a></li>' for v, label in options)
    stages = ""
    if tour and len(tour.stages) > 1:
        both = "Both stages" if len(tour.stages) == 2 else "All stages"
        chips = [(tour.id, both)] + [(st.id, st.label) for st in tour.stages]
        stages = '<nav class="stages">' + "".join(
            f'<a href="{page_url(sid, view)}"{" class=on" if sid == scope else ""}>{escape(label)}</a>'
            for sid, label in chips) + "</nav>"
    tabs = "".join(f'<a href="{page_url(scope, v)}"{" class=on" if v == view else ""}>{label}</a>'
                   for v, label in [*VIEW_LABELS.items(), ("matches", "Matches")])
    if not scoped:
        body = '<div class="card empty">No matches recorded yet.</div>'
    elif view == "matches":
        body = _matches_table(scoped)
    else:
        body = _ranking_table(rankings.team_stats(scoped), view)
    title = (tour.label + (f" · {stage.label}" if stage else "")) if tour else "All time"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pointsRanker · {escape(title)}</title><style>{CSS}</style></head><body><main>
<header><div><h1><a href="/">pointsRanker</a></h1><div class="sub">Team rankings from Twitch channel-point predictions</div></div>
<select onchange="location=this.value" aria-label="Tournament">{select}</select></header>
<noscript><details class="tours"><summary>Tournaments</summary><ul>{links}</ul></details></noscript>
{stages}<nav>{tabs}</nav>{_summary(scoped)}<p class="note">{NOTES[view]}</p>{body}
{FOOTER}</main></body></html>"""


def _scopes(tours: list[rankings.Tournament]) -> list[str]:
    return ["all"] + [sid for t in tours
                      for sid in [t.id, *(st.id for st in t.stages if len(t.stages) > 1)]]


def page_paths(conn: sqlite3.Connection, names: dict) -> list[str]:
    """Every page path the site has, for the static build."""
    tours = rankings.tournaments(rankings.load_matches(conn), names)
    return ["/"] + [page_url(sc, v) for sc in _scopes(tours) for v in VIEWS]


def logo_paths(conn: sqlite3.Connection) -> list[str]:
    return [f"/logo/{logo_slug(r['name'])}.png" for r in conn.execute("SELECT name FROM teams")]


def render(conn: sqlite3.Connection, path: str, names: dict) -> Response | None:
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
        if view not in VIEWS or t not in _scopes(tours):
            return None
    else:
        return None
    return Response(200, "text/html; charset=utf-8", _page(tours, matches, t, view).encode())


def not_found_page() -> bytes:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>pointsRanker · Not found</title>'
            f'<style>{CSS}</style></head><body><main><h1>Not found</h1>'
            f'<p class="note">That page doesn\'t exist. <a href="/">Back to the rankings</a>.</p>'
            f'{FOOTER}</main></body></html>').encode()


def make_handler(db_path: Path, names: dict):
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


def serve(db_path: Path, host: str, port: int, names: dict) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(db_path.resolve(), names))
    log.info("serving rankings on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
