# pointsRanker

Records the channel-point predictions on a Twitch channel (default: `fenriscreations`, the EVE
Online Alliance Tournament streams) into SQLite, and publishes a website that ranks teams by them:

- **Perceived**: a team "wins" a match when chat bet more points on it than on its opponent,
  whatever the real result. Perceived W-L plus an Elo rating over those results.
- **Points bet / won / lost**: total points bet on a team; points its backers took from the
  opposing pool when it won; points its backers lost when it lost.
- **Matches**: every match with both pools, the favourite and upsets.

Rankings are per tournament (matches more than 14 days apart start a new one) and all-time.
Team logos are the EVE alliance logos shown on evewho.com, or EVE's default alliance logo.

Python 3.10+, standard library only. Publishing needs Node (for `npx wrangler`).

## Commands

```sh
python -m pointsranker run [--publish]   # collect predictions live; --publish redeploys the site when one resolves
python -m pointsranker backfill [--since YYYY-MM-DD]  # import the channel's resolved prediction history
python -m pointsranker teams [--refresh] [--export-logos DIR]
python -m pointsranker serve [--port 8000]  # local preview at http://127.0.0.1:8000
python -m pointsranker build             # render the static site into site/
python -m pointsranker publish           # build + deploy to Cloudflare
```

Data lives in `data/predictions.db`. Two optional JSON files in the repo root tune the output:

- `tournaments.json`: names for tournaments, keyed by start date
  (`{"2026-08-29": "Alliance Tournament XXII"}`).
- `teams_overrides.json`: alliance ids for team names ESI can't resolve
  (`{"Lazerhawks": 99001234}`). Run `teams --refresh` after editing it.

## Hosting

The site is plain static files, served for free by a Cloudflare Worker with static assets
(`deploy/wrangler.toml`), at `https://pointsranker.<your-subdomain>.workers.dev`.

One-time setup:

1. `npx wrangler login`. Skip this if wrangler is already logged in to your Cloudflare account.
2. `python -m pointsranker backfill` (first time only), then `python -m pointsranker publish`.

To use a custom domain instead, add it to the `pointsranker` Worker in the Cloudflare dashboard
(Workers & Pages → pointsranker → Settings → Domains & Routes).

## Tournament weekends

Run the collector with `--publish`, so the site updates a minute or two after each prediction
resolves:

```sh
cp contrib/pointsranker.service ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user start pointsranker    # before the first stream
systemctl --user stop pointsranker     # after the last
journalctl --user -u pointsranker -f   # logs
```

While the channel is offline it polls every 2 minutes and does nothing else. If it's stopped
during a stream, nothing is lost for the rankings: Twitch keeps resolved predictions, and the
next start (or `backfill`) picks them up. Only the mid-prediction point snapshots are missed.

The prediction data comes from Twitch's web GraphQL API (the one twitch.tv itself uses). It is
undocumented and could change without notice.
