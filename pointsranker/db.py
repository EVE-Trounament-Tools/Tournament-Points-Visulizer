"""SQLite storage for predictions, outcome time series and teams."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id                 TEXT PRIMARY KEY,
    channel            TEXT NOT NULL,
    title              TEXT NOT NULL,
    status             TEXT NOT NULL,
    created_at         TEXT,
    locked_at          TEXT,
    ended_at           TEXT,
    window_seconds     INTEGER,
    winning_outcome_id TEXT,
    stream_id          TEXT,
    first_seen         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    id            TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL REFERENCES predictions(id),
    position      INTEGER NOT NULL,
    title         TEXT NOT NULL,
    team_name     TEXT REFERENCES teams(name),
    color         TEXT,
    total_points  INTEGER,
    total_users   INTEGER
);
CREATE INDEX IF NOT EXISTS outcomes_prediction ON outcomes(prediction_id);
CREATE TABLE IF NOT EXISTS outcome_snapshots (
    id           INTEGER PRIMARY KEY,
    outcome_id   TEXT NOT NULL REFERENCES outcomes(id),
    ts           TEXT NOT NULL,
    status       TEXT NOT NULL,
    total_points INTEGER,
    total_users  INTEGER
);
CREATE INDEX IF NOT EXISTS snapshots_outcome ON outcome_snapshots(outcome_id, ts);
CREATE TABLE IF NOT EXISTS teams (
    name            TEXT PRIMARY KEY,
    eve_id          INTEGER,
    eve_type        TEXT,
    evewho_url      TEXT,
    logo            BLOB NOT NULL,
    logo_is_default INTEGER NOT NULL,
    resolved_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_aliases (
    alias      TEXT PRIMARY KEY,  -- name as written in a prediction title
    team_name  TEXT NOT NULL REFERENCES teams(name),
    checked_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_snapshots(conn)
    conn.executescript(SCHEMA)
    return conn


def _migrate_snapshots(conn: sqlite3.Connection) -> None:
    """Early DBs keyed snapshots on (outcome_id, ts), which dropped same-instant changes."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(outcome_snapshots)")]
    if cols and "id" not in cols:
        conn.executescript("""
            ALTER TABLE outcome_snapshots RENAME TO outcome_snapshots_old;
            CREATE TABLE outcome_snapshots (
                id INTEGER PRIMARY KEY, outcome_id TEXT NOT NULL REFERENCES outcomes(id),
                ts TEXT NOT NULL, status TEXT NOT NULL, total_points INTEGER, total_users INTEGER);
            INSERT INTO outcome_snapshots (outcome_id, ts, status, total_points, total_users)
                SELECT outcome_id, ts, status, total_points, total_users FROM outcome_snapshots_old;
            DROP TABLE outcome_snapshots_old;
        """)


def upsert_prediction(conn: sqlite3.Connection, channel: str, pred: dict,
                      team_names: dict[str, str | None], stream_id: str | None = None,
                      snapshot: bool = True) -> tuple[str | None, bool]:
    """Insert or update a prediction and its outcomes.

    `team_names` maps outcome id -> team name (already present in `teams`).
    Returns (previous status or None if new, whether any outcome totals changed).
    """
    ts = now()
    row = conn.execute("SELECT status FROM predictions WHERE id = ?", (pred["id"],)).fetchone()
    prev_status = row["status"] if row else None
    winner = (pred.get("winningOutcome") or {}).get("id")
    conn.execute("""
        INSERT INTO predictions (id, channel, title, status, created_at, locked_at, ended_at,
                                 window_seconds, winning_outcome_id, stream_id, first_seen, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title, status = excluded.status, locked_at = excluded.locked_at,
            ended_at = excluded.ended_at, window_seconds = excluded.window_seconds,
            winning_outcome_id = excluded.winning_outcome_id,
            stream_id = COALESCE(predictions.stream_id, excluded.stream_id),
            updated_at = excluded.updated_at
    """, (pred["id"], channel, pred["title"], pred["status"], pred.get("createdAt"),
          pred.get("lockedAt"), pred.get("endedAt"), pred.get("predictionWindowSeconds"),
          winner, stream_id, ts, ts))

    changed = False
    for pos, o in enumerate(pred["outcomes"]):
        old = conn.execute("SELECT total_points, total_users FROM outcomes WHERE id = ?",
                           (o["id"],)).fetchone()
        conn.execute("""
            INSERT INTO outcomes (id, prediction_id, position, title, team_name, color,
                                  total_points, total_users)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title, team_name = excluded.team_name, color = excluded.color,
                total_points = excluded.total_points, total_users = excluded.total_users
        """, (o["id"], pred["id"], pos, o["title"], team_names.get(o["id"]), o.get("color"),
              o.get("totalPoints"), o.get("totalUsers")))
        if old is None or (old["total_points"], old["total_users"]) != (o.get("totalPoints"), o.get("totalUsers")):
            changed = True
            if snapshot:
                conn.execute("""INSERT INTO outcome_snapshots
                                (outcome_id, ts, status, total_points, total_users)
                                VALUES (?, ?, ?, ?, ?)""",
                             (o["id"], ts, pred["status"], o.get("totalPoints"), o.get("totalUsers")))
    conn.commit()
    return prev_status, changed


def get_team(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM teams WHERE name = ?", (name,)).fetchone()


def get_alias(conn: sqlite3.Connection, alias: str) -> sqlite3.Row | None:
    return conn.execute("""SELECT a.team_name, a.checked_at, t.eve_id FROM team_aliases a
                           JOIN teams t ON t.name = a.team_name WHERE a.alias = ?""", (alias,)).fetchone()


def save_alias(conn: sqlite3.Connection, alias: str, team_name: str) -> None:
    conn.execute("""INSERT INTO team_aliases VALUES (?, ?, ?)
                    ON CONFLICT(alias) DO UPDATE SET team_name = excluded.team_name,
                                                     checked_at = excluded.checked_at""",
                 (alias, team_name, now()))
    conn.commit()


def save_team(conn: sqlite3.Connection, name: str, eve_id: int | None, eve_type: str | None,
              evewho_url: str | None, logo: bytes, logo_is_default: bool) -> None:
    conn.execute("""
        INSERT INTO teams VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            eve_id = excluded.eve_id, eve_type = excluded.eve_type, evewho_url = excluded.evewho_url,
            logo = excluded.logo, logo_is_default = excluded.logo_is_default,
            resolved_at = excluded.resolved_at
    """, (name, eve_id, eve_type, evewho_url, logo, int(logo_is_default), now()))
    conn.commit()
