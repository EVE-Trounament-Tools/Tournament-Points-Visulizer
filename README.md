# pointsRanker

Records the channel-point predictions on a Twitch channel (default: `fenriscreations`, the EVE
Online Alliance Tournament streams) into SQLite, and publishes a website that ranks teams by them:

- **Perceived**: a team "wins" a match when chat bet more points on it than on its opponent,
  whatever the real result. Perceived W-L plus an Elo rating over those results.
- **Points bet / won / lost**: total points bet on a team; points its backers took from the
  opposing pool when it won; points its backers lost when it lost.
- **Matches**: every match with both pools, the favourite and upsets.

Rankings are per tournament and all-time. Matches split into stages wherever there's a break
of more than 14 days. A stage that starts within 75 days of the previous one's end joins it as
a later stage (feeders/trials/prelims, then finals, usually 1–2 months apart). A tournament with
several stages can be viewed per stage or with all stages combined.
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

- `tournaments.json`: names, keyed by a stage's start date. The value is either the tournament
  name (`"2026-04-11": "Anger Games 7"`) or an object with any of `name`, `stage` (the stage
  label, e.g. `"Feeders"`) and `new_tournament: true` (stop this stage joining the previous
  tournament). Stages without a label are named from their titles (Feeders/Trials/Prelims),
  and the last one is named Finals.
- `teams_overrides.json`: fixes for team names, keyed by the name as written in predictions
  (case-insensitive). Run `python -m pointsranker teams --refresh`, then `publish`, after
  editing it.
  - An alliance id, for a name ESI can't resolve: `"Lazerhawks": 99001234`.
  - Another team's name, to merge two names for the same team: `"UseCodeWIZARD": "Catlas Tor UseCodeWIZARD"`.
    Spellings that differ only in case, spacing or punctuation, or that are cut off at Twitch's
    25-character limit, are merged automatically.

## Hosting

The site is plain static files, served for free by a Cloudflare Worker with static assets
(`deploy/wrangler.toml`), at <https://pointsranker.ackbad-tournament-tools.workers.dev>
(generally `https://pointsranker.<your-subdomain>.workers.dev`).

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
unofficial and undocumented, is not endorsed by Twitch, and could change or stop working without notice.

## License

The code is MIT-licensed (see `LICENSE`). EVE Online and the EVE logo are the registered
trademarks of CCP hf. The alliance logos, including `pointsranker/assets/default_alliance_logo.png`,
are © CCP hf. and are not covered by the MIT license. This project is not affiliated with or
endorsed by CCP hf. or Twitch.
