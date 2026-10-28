"""Long-running collector: polls the channel and records predictions as they happen."""
import logging
import signal
import sqlite3
import threading

from . import db, site, teams, twitch

log = logging.getLogger(__name__)

POLL_ACTIVE = 10    # seconds, while a prediction is open/locked
POLL_LIVE = 30      # stream live, no prediction
POLL_OFFLINE = 120  # stream offline


def record(conn: sqlite3.Connection, channel: str, pred: dict, overrides: dict[str, int | str],
           stream_id: str | None = None) -> bool:
    """Store a prediction; True if it just became RESOLVED."""
    names = teams.team_names_for(conn, pred, overrides)
    prev, _ = db.upsert_prediction(conn, channel, pred, names, stream_id)
    if prev != pred["status"]:
        log.info("prediction %s: %s -> %s  %r", pred["id"][:8], prev or "new", pred["status"], pred["title"])
    return pred["status"] == "RESOLVED" and prev != "RESOLVED"


def run(conn: sqlite3.Connection, channel: str, overrides: dict[str, int | str],
        publisher: "site.Publisher | None" = None) -> None:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    was_live = None
    log.info("collecting predictions for %s (ctrl-c to stop)", channel)
    if publisher:
        publisher.request()  # bring the site in line with the DB at startup
    while not stop.is_set():
        try:
            state = twitch.fetch_live(channel)
        except Exception as e:  # never let a network hiccup kill the daemon
            log.error("poll failed: %s", e)
            stop.wait(POLL_OFFLINE)
            continue

        live = state["stream"] is not None
        if live != was_live:
            log.info("%s is %s", channel, "LIVE" if live else "offline")
            was_live = live
        stream_id = state["stream"]["id"] if live else None

        # Resolved first so a prediction that ended between polls gets its final totals.
        for pred in state["resolved"] + state["active"]:
            try:
                if record(conn, channel, pred, overrides, stream_id) and publisher:
                    publisher.request()
            except sqlite3.Error as e:
                log.error("failed to store prediction %s: %s", pred["id"], e)

        stop.wait(POLL_ACTIVE if state["active"] else POLL_LIVE if live else POLL_OFFLINE)
    if publisher:
        publisher.stop()
    log.info("stopped")


def backfill(conn: sqlite3.Connection, channel: str, overrides: dict[str, int | str],
             since: str | None = None) -> int:
    n = 0
    for pred in twitch.iter_resolved(channel):
        if since and (pred.get("createdAt") or "") < since:
            break
        names = teams.team_names_for(conn, pred, overrides)
        db.upsert_prediction(conn, channel, pred, names, snapshot=False)
        n += 1
        if n % 50 == 0:
            log.info("backfilled %d predictions (at %s)", n, pred.get("createdAt", "")[:10])
    return n
