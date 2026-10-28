"""Team rankings computed from stored predictions.

A "match" is a prediction whose two outcomes are both teams. The perceived
winner is the team with more channel points bet on it, regardless of result.
Points won/lost are from the backers' point of view: when a team actually wins
its backers take the opposing pool; when it loses they lose their own pool.
"""
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ELO_START = 1500.0
ELO_K = 32.0
STAKE = 1000  # channel points a "loyal bettor" puts on their team every match
STAGE_GAP = timedelta(days=14)    # a longer break between matches starts a new stage
FINALS_GAP = timedelta(days=75)   # a stage starting within this of the previous one's end belongs
                                  # to the same tournament (feeders -> finals are 1-2 months apart)


@dataclass
class Side:
    team: str
    points: int
    users: int


@dataclass
class Match:
    id: str
    title: str
    created_at: str
    status: str
    a: Side
    b: Side
    winner: str | None  # actual winning team

    @property
    def total(self) -> int:
        return self.a.points + self.b.points

    @property
    def perceived_winner(self) -> str | None:
        if self.a.points == self.b.points:
            return None
        return self.a.team if self.a.points > self.b.points else self.b.team

    @property
    def upset(self) -> bool:
        return bool(self.winner and self.perceived_winner and self.winner != self.perceived_winner)


@dataclass
class Stage:
    id: str            # "<tournament id>-<slug>", e.g. "2025-08-30-feeders"
    label: str | None  # None for an unnamed single-stage tournament
    matches: list[Match]


@dataclass
class Tournament:
    id: str            # start date of its first stage, YYYY-MM-DD
    label: str
    stages: list[Stage]

    @property
    def matches(self) -> list[Match]:
        return [m for s in self.stages for m in s.matches]


@dataclass
class Stats:
    team: str
    matches: int = 0
    p_wins: int = 0
    p_losses: int = 0
    share_sum: float = 0.0
    elo: float = ELO_START
    points_bet: int = 0
    users_bet: int = 0
    points_won: int = 0
    points_lost: int = 0
    wins: int = 0
    losses: int = 0
    bets: int = 0          # matches with a result, each backed with STAKE
    bet_profit: float = 0.0
    best_win: float = 0.0  # largest single net payout
    upset_wins: int = 0    # actual wins with the smaller pool

    @property
    def p_win_pct(self) -> float:
        decided = self.p_wins + self.p_losses
        return self.p_wins / decided if decided else 0.0

    @property
    def avg_share(self) -> float:
        return self.share_sum / self.matches if self.matches else 0.0

    @property
    def roi(self) -> float:
        return self.bet_profit / (self.bets * STAKE) if self.bets else 0.0


def bet_return(own: int, total: int, won: bool, stake: int = STAKE) -> float:
    """Net result of betting `stake` on a side with pool `own` out of `total`. Twitch pays
    winners the whole pool in proportion to their stake; the bet joins the pool itself."""
    return stake * (total + stake) / (own + stake) - stake if won else -stake


def load_matches(conn: sqlite3.Connection) -> list[Match]:
    rows = conn.execute("""
        SELECT p.id, p.title, p.created_at, p.status, p.winning_outcome_id,
               a.id AS a_id, a.team_name AS a_team, a.total_points AS a_pts, a.total_users AS a_users,
               b.id AS b_id, b.team_name AS b_team, b.total_points AS b_pts, b.total_users AS b_users
        FROM predictions p
        JOIN outcomes a ON a.prediction_id = p.id AND a.position = 0
        JOIN outcomes b ON b.prediction_id = p.id AND b.position = 1
        WHERE p.status = 'RESOLVED' AND a.team_name IS NOT NULL AND b.team_name IS NOT NULL AND a.team_name <> b.team_name
          AND (SELECT count(*) FROM outcomes o WHERE o.prediction_id = p.id) = 2
        ORDER BY p.created_at
    """).fetchall()
    matches = []
    for r in rows:
        winner = {r["a_id"]: r["a_team"], r["b_id"]: r["b_team"]}.get(r["winning_outcome_id"])
        matches.append(Match(
            r["id"], r["title"], r["created_at"], r["status"],
            Side(r["a_team"], r["a_pts"] or 0, r["a_users"] or 0),
            Side(r["b_team"], r["b_pts"] or 0, r["b_users"] or 0),
            winner if r["status"] == "RESOLVED" else None,
        ))
    return matches


def _date(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:19])


def load_names(path: Path | None) -> dict:
    """tournaments.json: keyed by a stage's start date (YYYY-MM-DD). A value is either the
    tournament's name, or {"name": ..., "stage": ..., "new_tournament": true} where every
    field is optional and new_tournament stops the stage joining the previous tournament."""
    return json.loads(path.read_text()) if path and path.exists() else {}


def _span(start: datetime, end: datetime) -> str:
    if start.year == end.year:
        return f"{start:%b %-d} – {end:%b %-d %Y}"
    return f"{start:%b %-d %Y} – {end:%b %-d %Y}"


def _stage_kind(matches: list[Match]) -> str | None:
    titles = " ".join(m.title.lower() for m in matches)
    for word, label in (("feeder", "Feeders"), ("trial", "Trials"), ("qualif", "Qualifiers"),
                        ("prelim", "Prelims")):
        if word in titles:
            return label
    return None


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "stage"


def tournaments(matches: list[Match], names: dict | None = None,
                stage_gap: timedelta = STAGE_GAP, finals_gap: timedelta = FINALS_GAP) -> list[Tournament]:
    """Group matches into stages (split on breaks longer than `stage_gap`) and stages into
    tournaments (a stage starting within `finals_gap` of the previous one joins it). Newest first."""
    names = names or {}

    def conf(stage: list[Match]) -> dict:
        v = names.get(_date(stage[0].created_at).strftime("%Y-%m-%d"), {})
        return {"name": v} if isinstance(v, str) else v

    stages: list[list[Match]] = []
    for m in matches:
        if stages and _date(m.created_at) - _date(stages[-1][-1].created_at) <= stage_gap:
            stages[-1].append(m)
        else:
            stages.append([m])

    groups: list[list[list[Match]]] = []
    for st in stages:
        joins = (groups and not conf(st).get("new_tournament")
                 and _date(st[0].created_at) - _date(groups[-1][-1][-1].created_at) <= finals_gap)
        if joins:
            groups[-1].append(st)
        else:
            groups.append([st])

    out = []
    for g in groups:
        start, end = _date(g[0][0].created_at), _date(g[-1][-1].created_at)
        tid = start.strftime("%Y-%m-%d")
        label = next((conf(st)["name"] for st in g if conf(st).get("name")), _span(start, end))
        built, seen = [], set()
        for i, st in enumerate(g):
            stage_label = conf(st).get("stage")
            if not stage_label and len(g) > 1:
                last = i == len(g) - 1
                stage_label = "Finals" if last else _stage_kind(st) or "Prelims"
            sid = f"{tid}-{_slug(stage_label or 'all')}"
            while sid in seen:
                sid += "-2"
            seen.add(sid)
            built.append(Stage(sid, stage_label, st))
        out.append(Tournament(tid, label, built))
    return out[::-1]


def team_stats(matches: list[Match]) -> dict[str, Stats]:
    stats: dict[str, Stats] = defaultdict(lambda: Stats(""))
    for m in matches:  # chronological, which Elo depends on
        sa, sb = stats[m.a.team], stats[m.b.team]
        sa.team, sb.team = m.a.team, m.b.team
        for s, own, other in ((sa, m.a, m.b), (sb, m.b, m.a)):
            s.matches += 1
            s.points_bet += own.points
            s.users_bet += own.users
            s.share_sum += own.points / m.total if m.total else 0.5
            if m.winner == own.team:
                s.wins += 1
                s.points_won += other.points
                s.upset_wins += own.points < other.points
            elif m.winner == other.team:
                s.losses += 1
                s.points_lost += own.points
            if m.winner:
                net = bet_return(own.points, m.total, m.winner == own.team)
                s.bets += 1
                s.bet_profit += net
                s.best_win = max(s.best_win, net)

        pw = m.perceived_winner
        if pw:
            winner, loser = (sa, sb) if pw == m.a.team else (sb, sa)
            winner.p_wins += 1
            loser.p_losses += 1
        result_a = 0.5 if pw is None else float(pw == m.a.team)
        expected_a = 1 / (1 + 10 ** ((sb.elo - sa.elo) / 400))
        delta = ELO_K * (result_a - expected_a)
        sa.elo += delta
        sb.elo -= delta
    return dict(stats)


VIEWS = {
    "perceived": lambda s: (-s.p_wins, -s.p_win_pct, -s.elo),
    "elo": lambda s: (-s.elo,),
    "bet": lambda s: (-s.points_bet,),
    "won": lambda s: (-s.points_won,),
    "lost": lambda s: (-s.points_lost,),
    "loyal": lambda s: (-s.roi, -s.bet_profit),
}


def ranked(stats: dict[str, Stats], view: str) -> list[Stats]:
    return sorted(stats.values(), key=lambda s: (*VIEWS[view](s), s.team.lower()))
