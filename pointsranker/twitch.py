"""Minimal client for Twitch's web GraphQL endpoint (the one twitch.tv itself uses)."""
import json
import logging
import time
import urllib.error
import urllib.request

GQL_URL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # public twitch.tv web client id

log = logging.getLogger(__name__)

PREDICTION_FIELDS = """
  id title status createdAt lockedAt endedAt predictionWindowSeconds
  outcomes { id title color totalPoints totalUsers }
  winningOutcome { id }
"""


class GQLError(RuntimeError):
    pass


def query(q: str, retries: int = 4) -> dict:
    body = json.dumps({"query": q}).encode()
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                GQL_URL, body, {"Client-Id": CLIENT_ID, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
            if data.get("errors"):
                raise GQLError(data["errors"])
            return data["data"]
        except (urllib.error.URLError, TimeoutError, GQLError, json.JSONDecodeError) as e:
            if attempt == retries:
                raise
            log.warning("GQL request failed (%s); retrying in %.0fs", e, delay)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def fetch_live(login: str, resolved: int = 10) -> dict:
    """Stream status, active predictions and the most recent resolved ones."""
    data = query(f"""{{ user(login: "{login}") {{
        id stream {{ id createdAt }}
        channel {{
          activePredictionEvents {{ {PREDICTION_FIELDS} }}
          resolvedPredictionEvents(first: {resolved}) {{ edges {{ node {{ {PREDICTION_FIELDS} }} }} }}
        }}
    }} }}""")
    user = data["user"]
    if user is None:
        raise GQLError(f"unknown channel {login!r}")
    ch = user["channel"]
    return {
        "stream": user["stream"],
        "active": ch["activePredictionEvents"] or [],
        "resolved": [e["node"] for e in ch["resolvedPredictionEvents"]["edges"]],
    }


def iter_resolved(login: str, page: int = 10):
    """Yield resolved predictions newest-first. Pages above ~25 return a service error."""
    after = None
    while True:
        cursor = f', after: "{after}"' if after else ""
        data = query(f"""{{ user(login: "{login}") {{ channel {{
            resolvedPredictionEvents(first: {page}{cursor}) {{
              pageInfo {{ hasNextPage }}
              edges {{ cursor node {{ {PREDICTION_FIELDS} }} }}
            }} }} }} }}""")
        conn = data["user"]["channel"]["resolvedPredictionEvents"]
        for edge in conn["edges"]:
            yield edge["node"]
        if not conn["pageInfo"]["hasNextPage"] or not conn["edges"]:
            return
        after = conn["edges"][-1]["cursor"]
