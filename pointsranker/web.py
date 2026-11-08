"""Local rankings website: stdlib HTTP server rendering pages straight from the DB."""
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
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
    "underrated": "Underrated",
    "tossups": "Tossups",
}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#14171c;--muted:#5f6670;--line:#e3e6ea;--accent:#2f6fde;
--accent-bg:#e8f0fd;--win:#1d8a4e;--loss:#c23b3b;--chip:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--fg:#e8eaed;--muted:#9aa1ab;
--line:#2a2f38;--accent:#6ea0ff;--accent-bg:#1c2740;--win:#4cc584;--loss:#ff7a7a;--chip:#222731}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
main.wide{max-width:1400px}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:end;justify-content:space-between;margin-bottom:18px}
h1{font-size:22px;margin:0}h1 a{color:inherit;text-decoration:none}
h2{font-size:16px;margin:22px 0 10px}
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
main.wide th,main.wide td{padding:8px}
main.wide td.wrap,main.wide .team span{white-space:normal;overflow-wrap:anywhere}
main.wide td.wrap{min-width:110px}main.wide .team{min-width:130px}
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
.sorter select,.sorter input{font:inherit;border:0;background:var(--card);color:var(--fg);padding:3px 4px;max-width:46vw}
.sorter input{width:9em;border-bottom:1px solid var(--line)}
.sorter button{font:inherit;border:0;background:var(--chip);color:var(--fg);border-radius:6px;padding:3px 8px;cursor:pointer;white-space:nowrap}
.sorter button.rm{background:transparent;color:var(--muted);padding:3px 6px}
.sorter button.add{background:transparent;border:1px dashed var(--line);color:var(--accent)}
.sorter button.reset{background:transparent;color:var(--muted);text-decoration:underline}
footer{margin-top:28px;color:var(--muted);font-size:12px}
"""

FOOTER = ("<footer>EVE Online and the EVE logo are the registered trademarks of CCP hf. Alliance logos "
          "are © CCP hf. This site is not affiliated with or endorsed by CCP hf. or Twitch.</footer>")

# Client-side multi-level sorting and filtering for tables. Both live in the URL hash
# (#sort=elo:d,points_bet:a&filter=matches:ge:5) so views can be shared, and are carried over
# to links for the same view (other tournaments/stages) but not to views with different metrics.
# Metrics with "sort" appear in the sort menus; every metric can be filtered on.
SORT_JS = r"""(() => {
const table = document.querySelector("table.sortable");
const box = document.getElementById("sort"), fbox = document.getElementById("filter");
const metrics = JSON.parse(document.getElementById("metrics").textContent);
const byKey = Object.fromEntries(metrics.map(m => [m.key, m]));
const sortable = metrics.filter(m => m.sort);
const view = table.dataset.view, def = table.dataset.default, tie = table.dataset.tie;
const rows = [...table.tBodies[0].rows];
const fromHash = name => (location.hash.match(new RegExp("[#&]" + name + "=([^&]*)")) || [])[1] || "";
const parse = str => str.split(",").map(x => x.split(":"))
  .filter(([k], i, all) => byKey[k] && byKey[k].sort && all.findIndex(([j]) => j === k) === i)
  .map(([key, d]) => ({key, dir: d === "a" ? "a" : "d"}));
let state = parse(decodeURIComponent(fromHash("sort")));
if (!state.length) state = parse(def);
const OPS = {ge: "≥", le: "≤", has: "contains", not: "doesn't contain"};
const opsFor = m => m.text ? ["has", "not"] : ["ge", "le"];
let filters = fromHash("filter").split(",").map(x => x.split(":"))
  .filter(([k, op]) => byKey[k] && opsFor(byKey[k]).includes(op))
  .map(([key, op, v]) => ({key, op, value: decodeURIComponent(v || "")}));
const enc = () => state.map(s => s.key + ":" + s.dir).join(",");
const encFilters = () => filters.filter(f => f.value.trim())
  .map(f => f.key + ":" + f.op + ":" + encodeURIComponent(f.value.trim())).join(",");
const get = (tr, k) => tr.getAttribute("data-" + k);
const text = k => byKey[k] ? byKey[k].text : k === "team";
function cmp(a, b) {
  for (const {key, dir} of [...state, {key: tie, dir: "a"}]) {
    const x = get(a, key), y = get(b, key);
    let c;
    if (x === "" || y === "") { if (x !== y) return x === "" ? 1 : -1; continue; }
    else if (text(key)) c = x.localeCompare(y);
    else c = parseFloat(x) - parseFloat(y);
    if (c) return dir === "a" ? c : -c;
  }
  return 0;
}
// The filter's value in the row attributes' units, or null to ignore the filter.
function target(f) {
  const m = byKey[f.key], v = f.value.trim();
  if (!v) return null;
  if (m.text) return v.toLowerCase();
  if (m.fmt === "date") {
    const t = Date.parse(v + "T00:00:00Z") / 1000;
    return isNaN(t) ? null : f.op === "le" ? t + 86399 : t;  // "on or before" includes the day
  }
  const n = parseFloat(v.replace(/[,\s%+]/g, ""));
  return isNaN(n) ? null : m.fmt === "pct" ? n / 100 : n;
}
function passes(tr) {
  return filters.every(f => {
    const t = target(f), x = get(tr, f.key);
    if (t === null) return true;
    if (text(f.key)) return x.toLowerCase().includes(t) === (f.op === "has");
    if (x === "") return false;  // n/a never passes a number filter
    return f.op === "ge" ? parseFloat(x) >= t : parseFloat(x) <= t;
  });
}
function el(tag, props, text) {
  const e = Object.assign(document.createElement(tag), props);
  if (text !== undefined) e.textContent = text;
  return e;
}
const count = el("span", {className: "lbl"});
function apply() {
  const body = table.tBodies[0], renumber = "renumber" in table.dataset;
  let shown = 0;
  rows.sort(cmp).forEach(r => {
    r.hidden = !passes(r);
    if (!r.hidden && ++shown && renumber) r.cells[0].textContent = shown;
    body.appendChild(r);
  });
  count.textContent = encFilters() ? `${shown} of ${rows.length} shown` : "";
  const parts = [enc() !== def && "sort=" + enc(), encFilters() && "filter=" + encFilters()].filter(Boolean);
  const hash = parts.length ? "#" + parts.join("&") : "";
  history.replaceState(null, "", location.pathname + location.search + hash);
  const keep = u => (u = u.split("#")[0], u.endsWith("/" + view + "/") ? u + hash : u);
  document.querySelectorAll('a[href^="/t/"]').forEach(a => a.setAttribute("href", keep(a.getAttribute("href"))));
  document.querySelectorAll("header select option").forEach(o => { o.value = keep(o.value); });
}
function renderSort() {
  const redo = () => { apply(); renderSort(); };
  box.replaceChildren(el("span", {className: "lbl"}, "Sort by"));
  state.forEach((s, i) => {
    if (i) box.append(el("span", {className: "lbl"}, "then"));
    const lvl = el("span", {className: "lvl"});
    const sel = el("select", {ariaLabel: "Sort level " + (i + 1)});
    for (const m of sortable) {
      const o = new Option(m.label, m.key, false, m.key === s.key);
      o.disabled = m.key !== s.key && state.some(x => x.key === m.key);
      sel.append(o);
    }
    sel.onchange = () => { s.key = sel.value; s.dir = byKey[s.key].dir; redo(); };
    const team = text(s.key), down = s.dir === "d";
    const dir = el("button", {type: "button", className: "dir",
      title: team ? (down ? "Z to A" : "A to Z") : (down ? "Highest first" : "Lowest first")},
      team ? (down ? "Z→A" : "A→Z") : (down ? "↓ high" : "↑ low"));
    dir.onclick = () => { s.dir = down ? "a" : "d"; redo(); };
    lvl.append(sel, dir);
    if (i) {
      const rm = el("button", {type: "button", className: "rm", title: "Remove", ariaLabel: "Remove sort level"}, "×");
      rm.onclick = () => { state.splice(i, 1); redo(); };
      lvl.append(rm);
    }
    box.append(lvl);
  });
  const free = sortable.find(m => !state.some(s => s.key === m.key));
  if (free) {
    const add = el("button", {type: "button", className: "add"}, "+ Then by");
    add.onclick = () => { state.push({key: free.key, dir: free.dir}); redo(); };
    box.append(add);
  }
  if (enc() !== def) {
    const reset = el("button", {type: "button", className: "reset"}, "Reset");
    reset.onclick = () => { state = parse(def); redo(); };
    box.append(reset);
  }
  box.hidden = false;
}
function renderFilters() {
  const redo = () => { apply(); renderFilters(); };
  fbox.replaceChildren(el("span", {className: "lbl"}, "Filter"));
  filters.forEach((f, i) => {
    const m = byKey[f.key], lvl = el("span", {className: "lvl"});
    const sel = el("select", {ariaLabel: "Filter " + (i + 1)});
    for (const x of metrics) sel.append(new Option(x.label, x.key, false, x.key === f.key));
    sel.onchange = () => {
      f.key = sel.value;
      if (!opsFor(byKey[f.key]).includes(f.op)) f.op = opsFor(byKey[f.key])[0];
      f.value = "";
      redo();
    };
    const op = el("select", {ariaLabel: "Comparison"});
    for (const o of opsFor(m)) op.append(new Option(OPS[o], o, false, o === f.op));
    op.onchange = () => { f.op = op.value; apply(); };
    const input = el("input", {type: m.fmt === "date" ? "date" : "text", value: f.value, ariaLabel: "Value",
      placeholder: m.text ? "text" : m.fmt === "pct" ? "%" : "number", inputMode: m.text ? "text" : "decimal"});
    input.oninput = () => { f.value = input.value; apply(); };
    const rm = el("button", {type: "button", className: "rm", title: "Remove", ariaLabel: "Remove filter"}, "×");
    rm.onclick = () => { filters.splice(i, 1); redo(); };
    lvl.append(sel, op, input, rm);
    fbox.append(lvl);
  });
  const add = el("button", {type: "button", className: "add"}, filters.length ? "+ And" : "+ Add filter");
  add.onclick = () => {
    const m = sortable.find(x => !x.text) || metrics[0];
    filters.push({key: m.key, op: opsFor(m)[0], value: ""});
    redo();
    [...fbox.querySelectorAll("input")].pop().focus();
  };
  fbox.append(add);
  if (filters.length) {
    const clear = el("button", {type: "button", className: "reset"}, "Clear");
    clear.onclick = () => { filters = []; redo(); };
    fbox.append(clear);
  }
  fbox.append(count);
  fbox.hidden = false;
}
apply();
renderSort();
renderFilters();
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


def _tiles(items: list[tuple[str, str]]) -> str:
    return '<div class="summary">' + "".join(
        f'<div class="stat"><b>{v}</b><span>{k}</span></div>' for v, k in items) + "</div>"


def _summary(matches: list[rankings.Match]) -> str:
    total = sum(m.total for m in matches)
    upsets = sum(m.upset for m in matches)
    decided = sum(1 for m in matches if m.winner and m.perceived_winner)
    return _tiles([(_fmt(len(matches)), "matches"), (_fmt(total), "points wagered"),
                   (f"{upsets / decided:.0%}" if decided else "–", "upsets (crowd got it wrong)"),
                   (_fmt(sum(m.tossup for m in matches)), "tossups")])


def _tossup_summary(matches: list[rankings.Match]) -> str:
    tossups = [m for m in matches if m.tossup]
    leaned = [m for m in tossups if m.winner and m.pool_leader]
    held = sum(m.winner == m.pool_leader for m in leaned)
    return _tiles([(_fmt(len(tossups)), "tossups"),
                   (f"{len(tossups) / len(matches):.0%}" if matches else "–", "of all matches"),
                   (_fmt(sum(m.total for m in tossups)), "points wagered"),
                   (f"{held / len(leaned):.0%}" if leaned else "–", "won by the bigger pool")])


@dataclass
class Metric:
    key: str
    label: str
    value: Callable[[rankings.Stats], float | None]  # sort value; None = n/a
    desc: bool = True                                  # default sort direction
    text: bool = False                                 # sort alphabetically, not numerically
    fmt: str | None = None                             # "pct" (a fraction, filtered as %) or "date"


def _spec(metrics: list[Metric], sortable: set[str]) -> str:
    """The metrics the table script can filter on; those in `sortable` can also be sorted by."""
    return json.dumps([{"key": m.key, "label": m.label, "dir": "d" if m.desc else "a", "text": m.text,
                        "fmt": m.fmt, "sort": m.key in sortable} for m in metrics])


SORTER = '<div class="sorter" id="sort" hidden></div><div class="sorter" id="filter" hidden></div>'


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


def _lost_pct(s: rankings.Stats) -> str:
    return f"{s.lost_pct:.0%}" if s.lost_pct is not None else "–"


TEAM = Metric("team", "Team", lambda s: None, desc=False, text=True)  # sorted by name, see _row_attrs
METRICS: dict[str, list[Metric]] = {
    # First, so ALL_METRICS takes shared keys from later views: "Tossups" is only right on this page.
    "tossups": [Metric("wins", "Actual wins", lambda s: s.wins),
                Metric("losses", "Actual losses", lambda s: s.losses, desc=False),
                Metric("win_pct", "Actual win %", lambda s: s.win_pct, fmt="pct"),
                Metric("matches", "Tossups", lambda s: s.matches),
                Metric("points_won", "Points won", lambda s: s.points_won),
                Metric("points_lost", "Points lost", lambda s: s.points_lost),
                Metric("roi", "ROI", lambda s: s.roi if s.bets else None, fmt="pct")],
    "perceived": [Metric("p_wins", "Perceived wins", lambda s: s.p_wins),
                  Metric("p_losses", "Perceived losses", lambda s: s.p_losses, desc=False),
                  Metric("p_pct", "Win %", lambda s: s.p_win_pct, fmt="pct"),
                  Metric("elo", "Elo", lambda s: s.elo),
                  Metric("share", "Avg share", lambda s: s.avg_share, fmt="pct"),
                  Metric("wins", "Actual wins", lambda s: s.wins),
                  Metric("losses", "Actual losses", lambda s: s.losses, desc=False),
                  Metric("matches", "Matches", lambda s: s.matches)],
    "bet": [Metric("points_bet", "Points bet", lambda s: s.points_bet),
            Metric("bettors", "Bettors", lambda s: s.users_bet),
            Metric("matches", "Matches", lambda s: s.matches),
            Metric("bet_avg", "Avg / match", lambda s: _per(s.points_bet, s.matches)),
            Metric("lost_pct", "% lost", lambda s: s.lost_pct, fmt="pct")],
    "won": [Metric("points_won", "Points won", lambda s: s.points_won),
            Metric("wins", "Actual wins", lambda s: s.wins),
            Metric("won_avg", "Avg / win", lambda s: _per(s.points_won, s.wins))],
    "lost": [Metric("points_lost", "Points lost", lambda s: s.points_lost),
             Metric("losses", "Actual losses", lambda s: s.losses),
             Metric("lost_avg", "Avg / loss", lambda s: _per(s.points_lost, s.losses)),
             Metric("lost_pct", "% lost", lambda s: s.lost_pct, fmt="pct")],
    "loyal": [Metric("roi", "ROI", lambda s: s.roi if s.bets else None, fmt="pct"),
              Metric("profit", "Profit", lambda s: s.bet_profit if s.bets else None),
              Metric("bets", "Bets", lambda s: s.bets),
              Metric("wins", "Actual wins", lambda s: s.wins),
              Metric("losses", "Actual losses", lambda s: s.losses, desc=False),
              Metric("upsets", "Upset wins", lambda s: s.upset_wins),
              Metric("best", "Best win", lambda s: s.best_win if s.wins else None)],
    "underrated": [Metric("gap", "Underrated by", lambda s: s.underrated, fmt="pct"),
                   Metric("called_pct", "Actual win % (no tossups)", lambda s: s.called_win_pct, fmt="pct"),
                   Metric("p_pct", "Predicted win %", lambda s: s.p_win_pct, fmt="pct"),
                   Metric("upsets", "Upset wins", lambda s: s.upset_wins),
                   Metric("matches", "Matches", lambda s: s.matches)],
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
            Column("Avg / match", lambda s: _fmt(s.points_bet / s.matches)),
            Column("% lost", _lost_pct)],
    "won": [Column("Points won", lambda s: _fmt(s.points_won)),
            Column("Actual wins", lambda s: str(s.wins)),
            Column("Avg / win", lambda s: _fmt(s.points_won / s.wins) if s.wins else "–")],
    "lost": [Column("Points lost", lambda s: _fmt(s.points_lost)),
             Column("Actual losses", lambda s: str(s.losses)),
             Column("Avg / loss", lambda s: _fmt(s.points_lost / s.losses) if s.losses else "–"),
             Column("% lost", _lost_pct)],
    "loyal": [Column("Bets", lambda s: str(s.bets)),
              Column("W-L", lambda s: f"{s.wins}-{s.losses}"),
              Column("Upset wins", lambda s: str(s.upset_wins)),
              Column("Profit", lambda s: _signed(s.bet_profit, _fmt(abs(s.bet_profit))) if s.bets else "–"),
              Column("ROI", lambda s: _signed(s.roi, f"{abs(s.roi):.0%}") if s.bets else "–"),
              Column("Best win", lambda s: f"+{_fmt(s.best_win)}" if s.wins else "–")],
    "underrated": [Column("Actual W-L", lambda s: f"{s.called_wins}-{s.called_losses}"),
                   Column("Actual win %", lambda s: f"{s.called_win_pct:.0%}" if s.called_win_pct is not None else "–"),
                   Column("Predicted W-L", lambda s: f"{s.p_wins}-{s.p_losses}"),
                   Column("Predicted win %", lambda s: f"{s.p_win_pct:.0%}" if s.p_wins + s.p_losses else "–"),
                   Column("Underrated by", lambda s: _signed(s.underrated, f"{abs(s.underrated) * 100:.0f} pts")
                          if s.underrated is not None else "–")],
    "tossups": [Column("Tossups", lambda s: str(s.matches)),
                Column("W-L", lambda s: f"{s.wins}-{s.losses}"),
                Column("Win %", lambda s: f"{s.win_pct:.0%}" if s.win_pct is not None else "–"),
                Column("Points won", lambda s: _fmt(s.points_won)),
                Column("Points lost", lambda s: _fmt(s.points_lost)),
                Column("ROI", lambda s: _signed(s.roi, f"{abs(s.roi):.0%}") if s.bets else "–")],
}
# Mirrors rankings.VIEWS, which gives the server-side (no-JS) order.
DEFAULT_SORT = {
    "perceived": "p_wins:d,p_pct:d,elo:d",
    "bet": "points_bet:d",
    "won": "points_won:d",
    "lost": "points_lost:d",
    "loyal": "roi:d,profit:d",
    "underrated": "gap:d,matches:d",
    "tossups": "wins:d,win_pct:d,matches:d",
}
# Every metric, so any view can be filtered on any of them (e.g. matches played on "Points won").
ALL_METRICS = list({m.key: m for ms in METRICS.values() for m in ms}.values())


def _row_attrs(s: rankings.Stats, metrics: list[Metric]) -> str:
    def raw(v):
        return "" if v is None else repr(round(v, 6)) if isinstance(v, float) else str(v)
    attrs = [f'data-team="{escape(s.team.lower())}"']
    attrs += [f'data-{m.key}="{raw(m.value(s))}"' for m in metrics]
    return " ".join(attrs)


def _ranking_table(stats: dict[str, rankings.Stats], view: str) -> str:
    metrics, cols = METRICS[view], COLUMNS[view]
    keys = {m.key for m in metrics}
    everything = [*metrics, *(m for m in ALL_METRICS if m.key not in keys)]
    spec = _spec([*everything, TEAM], keys | {TEAM.key})
    head = '<tr><th class="n">#</th><th>Team</th>' + "".join(f'<th class="n">{c.label}</th>' for c in cols) + "</tr>"
    rows = []
    for i, s in enumerate(rankings.ranked(stats, view), 1):
        rows.append(f'<tr {_row_attrs(s, everything)}><td class="n rank">{i}</td><td>{_team(s.team)}</td>'
                    + "".join(f'<td class="n">{c.show(s)}</td>' for c in cols) + "</tr>")
    return (f'{SORTER}<script type="application/json" id="metrics">{spec}</script>'
            f'<div class="card"><table class="sortable" data-view="{view}" data-default="{DEFAULT_SORT[view]}"'
            f' data-tie="team" data-renumber>'
            f'<thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<script>{SORT_JS}</script>')


MatchRow = tuple[rankings.Match, rankings.MatchInfo]
MATCH_METRICS = [
    Metric("date", "Date", lambda r: int(datetime.fromisoformat(r[0].created_at[:19]).replace(tzinfo=timezone.utc).timestamp()),
           fmt="date"),
    Metric("no", "Match #", lambda r: r[1].number),
    Metric("phase", "Prelim / Finals", lambda r: r[1].phase or "", desc=False, text=True),
    Metric("team_a", "Team 1", lambda r: r[0].a.team.lower(), desc=False, text=True),
    Metric("team_b", "Team 2", lambda r: r[0].b.team.lower(), desc=False, text=True),
    Metric("winner", "Winner", lambda r: (r[0].winner or "").lower(), desc=False, text=True),
    Metric("total", "Total points", lambda r: r[0].total),
    Metric("fav_share", "Favourite's share", lambda r: max(r[0].a.points, r[0].b.points) / r[0].total
           if r[0].total else 0.5, fmt="pct"),
    Metric("upset", "Upset", lambda r: int(r[0].upset)),
    Metric("tossup", "Tossup", lambda r: int(r[0].tossup)),
]
EITHER_TEAM = Metric("teams", "Either team", lambda r: f"{r[0].a.team} | {r[0].b.team}".lower(), text=True)
TOUR_METRIC = Metric("tour", "Tournament", lambda r: int(r[1].tournament.id.replace("-", "")))
MATCHES_DEFAULT_SORT = "date:d"


def _matches_table(matches: list[rankings.Match], info: dict[str, rankings.MatchInfo],
                   show_tour: bool) -> str:
    sortable = [TOUR_METRIC, *MATCH_METRICS] if show_tour else MATCH_METRICS
    metrics = [EITHER_TEAM, *sortable]
    head = ('<tr><th class="n">#</th>' + ("<th>Tournament</th>" if show_tour else "")
            + '<th>Stage</th><th>Date</th><th>Team</th><th class="n">Points</th><th class="n">Points</th>'
            '<th>Team</th><th>Winner</th><th></th></tr>')
    rows = []
    for seq, m in reversed(list(enumerate(matches))):
        mi, fav = info[m.id], m.perceived_winner
        badge = "<span class=badge>upset</span>" if m.upset else "<span class=badge>tossup</span>" if m.tossup else ""
        attrs = " ".join([f'data-seq="{seq}"', *(f'data-{x.key}="{escape(str(x.value((m, mi))))}"'
                                                  for x in metrics)])
        rows.append(
            f'<tr {attrs}><td class="n rank">{mi.number}</td>'
            + (f'<td class="wrap">{escape(mi.tournament.label)}</td>' if show_tour else "")
            + f'<td>{escape(mi.phase or "–")}</td><td>{escape(m.created_at[:10])}</td>'
            f'<td>{_team(m.a.team, fav == m.a.team)}</td><td class="n">{_fmt(m.a.points)}</td>'
            f'<td class="n">{_fmt(m.b.points)}</td><td>{_team(m.b.team, fav == m.b.team)}</td>'
            f'<td class="wrap">{escape(m.winner or "–")}</td>'
            f'<td>{badge}</td></tr>')
    return (f'{SORTER}<script type="application/json" id="metrics">{_spec(metrics, {m.key for m in sortable})}</script>'
            f'<div class="card"><table class="sortable" data-view="matches" data-default="{MATCHES_DEFAULT_SORT}"'
            f' data-tie="seq"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<script>{SORT_JS}</script>')


def _tossup_matches(tossups: list[rankings.Match], info: dict[str, rankings.MatchInfo],
                    show_tour: bool) -> str:
    """Plain newest-first list: the page's sortable table is the team table above it."""
    head = ('<tr><th class="n">#</th>' + ("<th>Tournament</th>" if show_tour else "")
            + '<th>Stage</th><th>Date</th><th>Team</th><th class="n">Points</th><th class="n">Points</th>'
            '<th>Team</th><th>Winner</th><th class="n">Gap</th></tr>')
    rows = []
    for m in reversed(tossups):
        mi = info[m.id]
        rows.append(
            f'<tr><td class="n rank">{mi.number}</td>'
            + (f'<td class="wrap">{escape(mi.tournament.label)}</td>' if show_tour else "")
            + f'<td>{escape(mi.phase or "–")}</td><td>{escape(m.created_at[:10])}</td>'
            f'<td>{_team(m.a.team)}</td><td class="n">{_fmt(m.a.points)}</td>'
            f'<td class="n">{_fmt(m.b.points)}</td><td>{_team(m.b.team)}</td>'
            f'<td class="wrap">{escape(m.winner or "–")}</td><td class="n">{m.margin * 100:.1f} pts</td></tr>')
    return f'<h2>Tossup matches</h2><div class="card"><table><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


def _tossups_body(scoped: list[rankings.Match], history: list[rankings.Match],
                  info: dict[str, rankings.MatchInfo], show_tour: bool) -> str:
    tossups = [m for m in scoped if m.tossup]
    if not tossups:
        return '<div class="card empty">No tossups here.</div>'
    return _ranking_table(rankings.team_stats(tossups, history), "tossups") + _tossup_matches(tossups, info, show_tour)


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    cache: str = "no-store"


PAGE_RE = re.compile(r"^/t/([\w-]+)/(\w+)/?$")
VIEWS = [*VIEW_LABELS, "matches"]
WIDE_VIEWS = {"matches", "tossups"}  # views listing matches, whose tables need more room
NOTES = {
    "perceived": "A team gets a perceived win when chat bet more channel points on it than on its "
                 "opponent, whatever the real result. Tossups give no perceived result and count as a draw "
                 "for Elo, which runs over perceived results in match order, across every tournament, so "
                 "it's each team's all-time rating.",
    "bet": "Total channel points bet on the team across its matches. % lost is the share of those "
           "points its backers lost.",
    "won": "Points the team's backers took from the opposing pool when the team actually won.",
    "lost": "Points the team's backers lost when the team actually lost. % lost is that as a share of "
            "all points bet on the team.",
    "loyal": f"Profit from betting {rankings.STAKE:,} points on this team in every match it played. "
             f"Wins pay out at the match's real odds (upsets pay big, heavy-favourite wins pay little); "
             f"every loss costs the full {rankings.STAKE:,}.",
    "underrated": "How much better a team actually did than chat expected: its actual win rate minus its "
                  "predicted win rate (the share of its matches where chat bet more on it), leaving out "
                  "tossups. Teams with few matches swing a lot; filter on Matches to hide them.",
    "tossups": f"Matches where the two sides' shares of the points were under "
               f"{rankings.TOSSUP_MARGIN * 100:.0f} percentage points apart, so chat had no real favourite. "
               f"They don't count as upsets or perceived results; points and actual results still count "
               f"everywhere else. This table covers tossups only.",
    "matches": "Matches are numbered in order within their tournament. Bold team = crowd favourite "
               "(more points). Upset = favourite lost. Tossup = pools too close to name a favourite.",
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


SITE_NAME = "Twitch Points Ranker"
SITE_URL = "https://pointsranker.ackbad-tournament-tools.workers.dev"  # absolute URLs in link previews


def _meta(title: str, desc: str, url: str, image: str | None) -> str:
    """Link-preview tags (Discord, Slack, etc. read Open Graph; theme-color tints Discord's embed)."""
    tags = [("name", "description", desc), ("property", "og:site_name", SITE_NAME),
            ("property", "og:type", "website"), ("property", "og:title", title),
            ("property", "og:description", desc), ("property", "og:url", url),
            ("name", "twitter:card", "summary"), ("name", "theme-color", "#2f6fde")]
    if image:
        tags.append(("property", "og:image", image))
    return "".join(f'<meta {a}="{k}" content="{escape(v)}">' for a, k, v in tags)


def _preview(scoped: list[rankings.Match], history: list[rankings.Match], view: str) -> tuple[str, str | None]:
    """(description, #1 team) for a page's link preview."""
    if not scoped:
        return "No matches recorded yet.", None
    if view == "tossups":
        tossups = [m for m in scoped if m.tossup]
        if not tossups:
            return "No tossups here.", None
        top = rankings.ranked(rankings.team_stats(tossups, history), view)
        lead = " · ".join(f"{i}. {s.team}" for i, s in enumerate(top[:3], 1))
        return f"{lead} — {_fmt(len(tossups))} tossups out of {_fmt(len(scoped))} matches", top[0].team
    stats = rankings.team_stats(scoped, history)
    top = rankings.ranked(stats, "perceived" if view == "matches" else view)
    upsets = sum(m.upset for m in scoped)
    decided = sum(1 for m in scoped if m.winner and m.perceived_winner)
    line = f"{_fmt(len(scoped))} matches · {_fmt(sum(m.total for m in scoped))} points wagered" + (
        f" · {upsets / decided:.0%} upsets" if decided else "")
    if view == "matches":
        m = scoped[-1]
        lead = f"Latest: {m.a.team} vs {m.b.team}" + (f", {m.winner} won" if m.winner else "")
    else:
        lead = " · ".join(f"{i}. {s.team}" for i, s in enumerate(top[:3], 1))
    return f"{lead} — {line}", top[0].team


_ROOT_ATTR_RE = re.compile(r'\b(href|src|value)="/')


def _base_path(base_url: str) -> str:
    """The path part of base_url ("" for a site at a domain's root), e.g. "/tournament-bets"."""
    return urlparse(base_url).path.rstrip("/")


def _with_base(html: str, base_url: str) -> str:
    """Prefix the site's root-absolute links with base_url's path, for a site hosted below a domain's root."""
    prefix = _base_path(base_url)
    if not prefix:
        return html
    html = _ROOT_ATTR_RE.sub(lambda m: f'{m.group(1)}="{prefix}/', html)
    return html.replace("""a[href^="/t/"]""", f"""a[href^="{prefix}/t/"]""")


def _page(tours: list[rankings.Tournament], all_matches: list[rankings.Match], scope: str, view: str,
          path: str = "/", base_url: str = SITE_URL) -> str:
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
        body = _matches_table(scoped, rankings.match_info(tours), tour is None)
    elif view == "tossups":
        body = _tossups_body(scoped, all_matches, rankings.match_info(tours), tour is None)
    else:
        body = _ranking_table(rankings.team_stats(scoped, all_matches), view)
    summary = _tossup_summary(scoped) if view == "tossups" else _summary(scoped)
    main_class = ' class="wide"' if view in WIDE_VIEWS else ""
    title = (tour.label + (f" · {stage.label}" if stage else "")) if tour else "All time"
    desc, leader = _preview(scoped, all_matches, view)
    base = base_url.rstrip("/")
    image = f"{base}/logo/{logo_slug(leader)}.png" if leader else None
    meta = _meta(f"{title} · {dict(VIEW_LABELS, matches='Matches')[view]}", desc, base + path, image)
    return _with_base(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SITE_NAME} · {escape(title)}</title>{meta}<style>{CSS}</style></head><body><main{main_class}>
<header><div><h1><a href="/">{SITE_NAME}</a></h1><div class="sub">Team rankings from Twitch channel-point predictions</div></div>
<select onchange="location=this.value" aria-label="Tournament">{select}</select></header>
<noscript><details class="tours"><summary>Tournaments</summary><ul>{links}</ul></details></noscript>
{stages}<nav>{tabs}</nav>{summary}<p class="note">{NOTES[view]}</p>{body}
{FOOTER}</main></body></html>""", base_url)


def _scopes(tours: list[rankings.Tournament]) -> list[str]:
    return ["all"] + [sid for t in tours
                      for sid in [t.id, *(st.id for st in t.stages if len(t.stages) > 1)]]


def page_paths(conn: sqlite3.Connection, names: dict) -> list[str]:
    """Every page path the site has, for the static build."""
    tours = rankings.tournaments(rankings.load_matches(conn), names)
    return ["/"] + [page_url(sc, v) for sc in _scopes(tours) for v in VIEWS]


def logo_paths(conn: sqlite3.Connection) -> list[str]:
    return [f"/logo/{logo_slug(r['name'])}.png" for r in conn.execute("SELECT name FROM teams")]


def render(conn: sqlite3.Connection, path: str, names: dict, base_url: str = SITE_URL) -> Response | None:
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
    return Response(200, "text/html; charset=utf-8", _page(tours, matches, t, view, path, base_url).encode())


def not_found_page(base_url: str = SITE_URL) -> bytes:
    return _with_base(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{SITE_NAME} · Not found</title>'
            f'<meta property="og:site_name" content="{SITE_NAME}"><style>{CSS}</style></head><body><main><h1>Not found</h1>'
            f'<p class="note">That page doesn\'t exist. <a href="/">Back to the rankings</a>.</p>'
            f'{FOOTER}</main></body></html>', base_url).encode()


def make_handler(db_path: Path, names: dict, base_url: str = SITE_URL):
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
                    r = render(conn, url.path, names, base_url)
                finally:
                    conn.close()
            except sqlite3.Error as e:
                log.exception("request failed")
                r = Response(500, "text/plain", f"database error: {e}".encode())
            self._send(r or Response(404, "text/html; charset=utf-8", not_found_page(base_url)))

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

    return Handler


def serve(db_path: Path, host: str, port: int, names: dict) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(db_path.resolve(), names, f"http://{host}:{port}"))
    log.info("serving rankings on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
