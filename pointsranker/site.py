"""Build the rankings site as static files and publish it to Cloudflare."""
import logging
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import web

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
WRANGLER_CONFIG = ROOT / "deploy" / "wrangler.toml"
WRANGLER = ["npx", "--yes", "wrangler@4.148.0"]
PUBLISH_INTERVAL = 120  # seconds; at most one deploy this often


def _file_for(out: Path, path: str) -> Path:
    return out / (path.lstrip("/") + "index.html" if path.endswith("/") else path.lstrip("/"))


def build(db_path: Path, out: Path, names: dict, base_url: str = web.SITE_URL) -> int:
    """Render every page and logo into `out`, replacing it atomically. Returns files written."""
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".site-", dir=out.parent))
    try:
        paths = web.page_paths(conn, names) + web.logo_paths(conn)
        for path in paths:
            r = web.render(conn, path, names, base_url)
            if r is None:
                raise RuntimeError(f"no content for {path}")
            f = _file_for(tmp, path)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(r.body)
        (tmp / "404.html").write_bytes(web.not_found_page())
        old = out.with_name(out.name + ".old")
        if out.exists():
            out.rename(old)
        tmp.rename(out)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return len(paths) + 1


def deploy(out: Path) -> None:
    """Upload the built site with wrangler (only changed files are sent)."""
    if not out.joinpath("index.html").exists():
        raise RuntimeError(f"{out} has no built site; run build first")
    subprocess.run([*WRANGLER, "deploy", "--config", str(WRANGLER_CONFIG)], check=True, cwd=ROOT)


def publish(db_path: Path, out: Path, names: dict, base_url: str = web.SITE_URL) -> None:
    n = build(db_path, out, names, base_url)
    log.info("built %d files into %s; deploying", n, out)
    deploy(out)
    log.info("published")


class Publisher:
    """Background publisher for the collector: `request()` is cheap and never blocks;
    deploys run on their own thread, at most once per PUBLISH_INTERVAL."""

    def __init__(self, db_path: Path, out: Path, names: dict, base_url: str = web.SITE_URL):
        self.args = (db_path, out, names, base_url)
        self.wanted = threading.Event()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="publisher", daemon=True)
        self.thread.start()

    def request(self) -> None:
        self.wanted.set()

    def stop(self) -> None:
        self.stopping.set()
        self.wanted.set()
        self.thread.join(timeout=300)

    def _loop(self) -> None:
        last = 0.0
        while True:
            self.wanted.wait()
            if self.stopping.is_set():
                return
            wait = last + PUBLISH_INTERVAL - time.monotonic()
            if wait > 0 and self.stopping.wait(wait):
                return
            self.wanted.clear()
            last = time.monotonic()
            try:
                publish(*self.args)
            except Exception as e:  # keep collecting; the next resolved prediction retries
                log.error("publish failed: %s", e)
