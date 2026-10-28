"""python -m pointsranker {run | backfill | teams | serve | build | publish}"""
import argparse
import logging
from pathlib import Path

from . import collector, db, rankings, site, teams, web


def main() -> None:
    p = argparse.ArgumentParser(prog="pointsranker")
    p.add_argument("--channel", default="fenriscreations")
    p.add_argument("--db", type=Path, default=Path("data/predictions.db"))
    p.add_argument("--overrides", type=Path, default=Path("teams_overrides.json"),
                   help='JSON {"Team Name": alliance_id} for names ESI can\'t resolve')
    sub = p.add_subparsers(dest="cmd", required=True)
    rn = sub.add_parser("run", help="poll the channel and record predictions live")
    rn.add_argument("--publish", action="store_true",
                    help="rebuild and deploy the site whenever a prediction resolves")
    bf = sub.add_parser("backfill", help="import the channel's resolved prediction history")
    bf.add_argument("--since", help="stop at predictions created before this date (YYYY-MM-DD)")
    tm = sub.add_parser("teams", help="list teams and their EVE resolution")
    tm.add_argument("--refresh", action="store_true", help="re-resolve every team")
    tm.add_argument("--export-logos", type=Path, metavar="DIR", help="write logos as PNG files")
    p.add_argument("--names", type=Path, default=Path("tournaments.json"),
                   help='JSON {"YYYY-MM-DD": "Tournament name"} keyed by tournament start date')
    p.add_argument("--site", type=Path, default=Path("site"), help="static site output directory")
    p.add_argument("--base-url", default=web.SITE_URL,
                   help="public site URL, for absolute links in link previews (Discord etc.)")
    sv = sub.add_parser("serve", help="run the rankings website locally")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sub.add_parser("build", help="render the site as static files into --site")
    sub.add_parser("publish", help="build the site and deploy it to Cloudflare")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect(args.db)
    overrides = teams.load_overrides(args.overrides)

    names = rankings.load_names(args.names)

    if args.cmd == "serve":
        web.serve(args.db, args.host, args.port, names)
    elif args.cmd == "build":
        print(f"wrote {site.build(args.db, args.site, names, args.base_url)} files to {args.site}")
    elif args.cmd == "publish":
        site.publish(args.db, args.site, names, args.base_url)
    elif args.cmd == "run":
        publisher = site.Publisher(args.db, args.site, names, args.base_url) if args.publish else None
        collector.run(conn, args.channel, overrides, publisher)
    elif args.cmd == "backfill":
        n = collector.backfill(conn, args.channel, overrides, args.since)
        print(f"stored {n} predictions in {args.db}")
    elif args.cmd == "teams":
        if args.refresh:
            teams.refresh_all(conn, overrides)
        if args.export_logos:
            args.export_logos.mkdir(parents=True, exist_ok=True)
        for r in conn.execute("SELECT * FROM teams ORDER BY name"):
            flag = "default logo" if r["logo_is_default"] else "logo"
            print(f"{r['name']:<40} {r['eve_id'] or '-':>12}  {flag:<12} {r['evewho_url'] or ''}")
            if args.export_logos:
                safe = "".join(c if c.isalnum() else "_" for c in r["name"])
                (args.export_logos / f"{safe}.png").write_bytes(r["logo"])


if __name__ == "__main__":
    main()
