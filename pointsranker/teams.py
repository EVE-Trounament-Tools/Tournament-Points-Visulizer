"""Map prediction outcomes to EVE alliances and fetch their logos.

Logos are the ones evewho.com displays (served by images.evetech.net). Teams
that can't be resolved, or whose alliance has no logo, get EVE's default
alliance logo (a large star ringed by smaller stars).
"""
import difflib
import hashlib
import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

ESI_IDS = "https://esi.evetech.net/latest/universe/ids/?datasource=tranquility"
EVEWHO_API = "https://evewho.com/api/allilist/{id}"
EVEWHO_PAGE = "https://evewho.com/alliance/{id}"
LOGO_URL = "https://images.evetech.net/alliances/{id}/logo?size=128"
USER_AGENT = "pointsRanker (github.com/EVE-Tournament-Tools/Tournament-Points-Visualizer)"

DEFAULT_LOGO = (Path(__file__).parent / "assets" / "default_alliance_logo.png").read_bytes()
DEFAULT_LOGO_MD5 = hashlib.md5(DEFAULT_LOGO).hexdigest()
RETRY_AFTER = timedelta(hours=12)  # re-check unresolved names / default logos this often

RECORD_RE = re.compile(r"\s*\(\d+\s*-\s*\d+\)\s*$")  # trailing "(0-6)" win-loss record
VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
MIN_MATCH = 0.6
# Older match predictions name the teams only in the outcomes, e.g. "ATXX Match 10 Outcome",
# "Winner of ATXXI Feeders Match 65", "Finals 3/5 (...)", or bracket codes like "D-1" / "45A".
MATCH_TITLE_RE = re.compile(
    r"match|winner|outcome|who will win|finals?\b|\bplace\b|^\s*[A-Z]{1,2}-?\d+[A-Z]?\s*$|^\s*\d{1,3}[A-Z]\s*$",
    re.IGNORECASE)


def clean_name(title: str) -> str:
    return RECORD_RE.sub("", title).strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def parse_sides(prediction_title: str) -> list[str] | None:
    """Team names from titles like "14:30|M73; 34-yrs-old men vs ZERG REBORN (2-1)"."""
    match = re.split(r"[;|]", prediction_title)[-1]
    sides = [clean_name(s) for s in VS_RE.split(match)]
    return sides if len(sides) == 2 and all(sides) else None


def assign_sides(outcome_titles: list[str], sides: list[str]) -> list[str | None]:
    """Match each outcome to a side of the title. Mods type outcomes freehand
    ("TestAlliancePleaseIgnore", joke names), so match fuzzily and, with two
    outcomes, let the confident one decide the other."""
    if len(outcome_titles) == 2:
        straight = _similarity(outcome_titles[0], sides[0]) + _similarity(outcome_titles[1], sides[1])
        swapped = _similarity(outcome_titles[0], sides[1]) + _similarity(outcome_titles[1], sides[0])
        if max(straight, swapped) < MIN_MATCH:
            return [None, None]
        return list(sides) if straight >= swapped else [sides[1], sides[0]]
    out = []
    for title in outcome_titles:
        best = max(sides, key=lambda s: _similarity(title, s))
        out.append(best if _similarity(title, best) >= MIN_MATCH else None)
    return out


def _http(url: str, data: bytes | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=15) as r:
        return r.read()


_logos: dict[int, tuple[bytes, bool]] = {}  # alliance id -> (logo, is the default logo)


def alliance_logo(eve_id: int) -> tuple[bytes, bool]:
    if eve_id not in _logos:
        fetched = _http(LOGO_URL.format(id=eve_id))
        default = hashlib.md5(fetched).hexdigest() == DEFAULT_LOGO_MD5
        _logos[eve_id] = (DEFAULT_LOGO, True) if default else (fetched, False)
    return _logos[eve_id]


def lookup_alliance(*names: str) -> tuple[int, str] | None:
    """(alliance id, canonical name) for the first of `names` ESI knows as an alliance
    (exact, case-insensitive), preferring one with its own logo over the default."""
    variants = list(dict.fromkeys(v for n in names for v in (n, n.rstrip("."), n.rstrip(".") + ".")))
    data = json.loads(_http(ESI_IDS, json.dumps(variants).encode()))
    by_name = {a["name"].lower(): a for a in data.get("alliances", [])}
    found = list({a["id"]: a for v in variants if (a := by_name.get(v.lower()))}.values())
    if not found:
        return None
    best = next((a for a in found if not alliance_logo(a["id"])[1]), found[0])
    return best["id"], best["name"]


def match_known(conn: sqlite3.Connection, name: str) -> tuple[int, str] | None:
    """A resolved team this name is a variant of ("Test Alliance", "BrightSideofDeath")."""
    n = _norm(name)
    if len(n) < 6:
        return None
    for row in conn.execute("SELECT name, eve_id FROM teams WHERE eve_id IS NOT NULL"):
        k = _norm(row["name"])
        if n == k or (n in k or k in n) and min(len(n), len(k)) >= 8 or _similarity(n, k) >= 0.9:
            return row["eve_id"], row["name"]
    return None


TRUNCATED_MIN = 12  # Twitch cuts outcome names at 25 chars; a shared prefix this long is the same team


def same_team(a: str, b: str) -> bool:
    """Spellings of one team: equal ignoring case, spaces and punctuation ("Barcode" /
    "BARCODE"), or one cut off from the other ("Spooky Action at a Distan" / "...a Dist.")."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or min(len(a), len(b)) >= TRUNCATED_MIN and (a.startswith(b) or b.startswith(a))


def match_unresolved(conn: sqlite3.Connection, name: str) -> str | None:
    """An unresolved team this name is a spelling of; the most-used spelling wins."""
    for row in conn.execute("""
            SELECT t.name FROM teams t LEFT JOIN outcomes o ON o.team_name = t.name
            WHERE t.eve_id IS NULL GROUP BY t.name ORDER BY count(o.id) DESC, t.name"""):
        if same_team(name, row["name"]):
            return row["name"]
    return None


def evewho_name(alliance_id: int) -> str | None:
    info = json.loads(_http(EVEWHO_API.format(id=alliance_id))).get("info") or []
    return info[0]["name"] if info else None


def load_overrides(path: Path | None) -> dict[str, int | str]:
    """teams_overrides.json: name as written in predictions -> either an alliance id (for names
    ESI can't resolve) or another team name (the same team under a very different name)."""
    if path and path.exists():
        return {k.lower(): v for k, v in json.loads(path.read_text()).items()}
    return {}


def _older_than(ts: str, age: timedelta) -> bool:
    return datetime.fromisoformat(ts) < datetime.now(timezone.utc) - age


def _save_logo(conn: sqlite3.Connection, name: str, eve_id: int | None, url: str | None) -> None:
    logo, is_default = alliance_logo(eve_id) if eve_id else (DEFAULT_LOGO, True)
    db.save_team(conn, name, eve_id, "alliance" if eve_id else None, url, logo, is_default)
    log.info("team %r -> %s%s", name, eve_id or "unresolved", " (default logo)" if is_default else "")


def _group(alias: str, overrides: dict[str, int | str]) -> tuple[str | None, list[str]]:
    """(override target, every name known to be that team) for `alias`.

    Override targets are canonical: names mapped to them, and other spellings of them
    ("xqty" for "XQTY"), belong to their group. Returns (None, [alias]) for other names."""
    targets = sorted({t.strip() for t in overrides.values() if isinstance(t, str)})
    target = overrides.get(alias.lower())
    if isinstance(target, str):
        canon = target.strip()
        for _ in range(5):  # follow A -> B -> C; a loop just stops somewhere
            nxt = overrides.get(canon.lower())
            if not isinstance(nxt, str) or nxt.strip().lower() == canon.lower():
                break
            canon = nxt.strip()
    elif alias.strip() in targets:
        canon = alias.strip()
    else:
        canon = next((t for t in targets if same_team(alias, t)), None)
    if canon is None:
        return None, [alias]
    members = [canon, *(k for k, v in overrides.items() if isinstance(v, str) and v.strip() == canon), alias]
    return canon, list(dict.fromkeys(members))


def ensure_team(conn: sqlite3.Connection, alias: str, overrides: dict[str, int | str],
                force: bool = False) -> str:
    """Resolve a name from a prediction title to a stored team; returns the team name.

    Resolved teams are stored under their canonical alliance name, so spelling variants
    ("PUNISHING GRAY RAVEN", "Punishing Gray Raven") share one row. When several names are
    the same team (overrides), every one of them is looked up and the team takes the name of
    the alliance with its own logo, e.g. Barcode is the alliance "IIIIIIIIIIIIIIIIIIIIIIIII".
    """
    row = db.get_alias(conn, alias)
    if row and not force and (row["eve_id"] or not _older_than(row["checked_at"], RETRY_AFTER)):
        team = db.get_team(conn, row["team_name"])
        if not (team["logo_is_default"] and _older_than(team["resolved_at"], RETRY_AFTER)):
            return row["team_name"]
    canon, members = _group(alias, overrides)
    try:
        ids = [v for m in members if isinstance(v := overrides.get(m.lower()), int)]
        if ids:
            found = (ids[0], evewho_name(ids[0]) or canon or alias)
        else:
            found = lookup_alliance(*members) or (None if canon else match_known(conn, alias))
        if found:
            eve_id, name = found
        else:
            eve_id, name = None, canon or match_unresolved(conn, alias) or alias
        team = db.get_team(conn, name)
        if force or team is None or (team["logo_is_default"] and _older_than(team["resolved_at"], RETRY_AFTER)):
            _save_logo(conn, name, eve_id, EVEWHO_PAGE.format(id=eve_id) if eve_id else None)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("team lookup for %r failed: %s", alias, e)
        if row:
            return row["team_name"]
        name = canon or alias
        if db.get_team(conn, name) is None:
            db.save_team(conn, name, None, None, None, DEFAULT_LOGO, True)
    db.save_alias(conn, alias, name)
    return name


def team_names_for(conn: sqlite3.Connection, pred: dict, overrides: dict[str, int | str]) -> dict[str, str | None]:
    """outcome id -> team name, resolving any new teams along the way."""
    sides = parse_sides(pred["title"])
    titles = [o["title"] for o in pred["outcomes"]]
    if sides:
        matched = assign_sides(titles, sides)
    elif len(titles) == 2 and MATCH_TITLE_RE.search(pred["title"]):
        matched = [clean_name(t) or None for t in titles]
    else:
        matched = [None] * len(titles)
    return {o["id"]: ensure_team(conn, side, overrides) if side else None
            for o, side in zip(pred["outcomes"], matched)}


def refresh_all(conn: sqlite3.Connection, overrides: dict[str, int | str]) -> None:
    """Re-resolve every known name (e.g. after editing overrides), re-point stored
    outcomes at the result and drop teams nothing refers to any more."""
    for (alias,) in conn.execute("SELECT alias FROM team_aliases").fetchall():
        ensure_team(conn, alias, overrides, force=True)
    for pid, title in conn.execute("SELECT id, title FROM predictions").fetchall():
        outcomes = [dict(id=r[0], title=r[1]) for r in conn.execute(
            "SELECT id, title FROM outcomes WHERE prediction_id = ? ORDER BY position", (pid,))]
        names = team_names_for(conn, {"title": title, "outcomes": outcomes}, overrides)
        conn.executemany("UPDATE outcomes SET team_name = ? WHERE id = ?",
                         [(name, oid) for oid, name in names.items()])
    conn.execute("""DELETE FROM teams WHERE name NOT IN (SELECT team_name FROM team_aliases)
                    AND name NOT IN (SELECT team_name FROM outcomes WHERE team_name IS NOT NULL)""")
    conn.commit()
