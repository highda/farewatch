"""Deal feeds (RSS/Atom): keyword-filtered, deduped, fetched rarely and with conditional GET.

Only feeds that publish an open feed and don't block bots. (fly4free.com and secretflying.com sit behind
Cloudflare challenges -> intentionally not used.)
"""
from __future__ import annotations

import datetime as dt
import html
import re
import unicodedata
import xml.etree.ElementTree as ET

from ..db import iso
from ..http import HttpError
from .base import Context


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_feed(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    tag = lambda e: e.tag.rsplit("}", 1)[-1]
    items = []
    for it in root.iter():
        if tag(it) not in ("item", "entry"):
            continue
        f: dict[str, str] = {}
        for c in it:
            t = tag(c)
            if t == "link":
                f.setdefault("link", c.get("href") or _text(c))
            elif t in ("title", "guid", "id", "pubDate", "published", "updated", "description", "summary", "content"):
                f.setdefault(t, _text(c))
        summary = re.sub(r"<[^>]+>", " ", html.unescape(f.get("description") or f.get("summary") or f.get("content") or ""))
        link = f.get("link", "")
        items.append({
            "guid": f.get("guid") or f.get("id") or link,
            "title": html.unescape(f.get("title", "")).strip(),
            "url": link if link.startswith(("http://", "https://")) else "",
            "published": f.get("pubDate") or f.get("published") or f.get("updated") or "",
            "summary": re.sub(r"\s+", " ", summary).strip()[:300],
        })
    return [i for i in items if i["guid"] and i["title"]]


def matches(item: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    hay = fold(item["title"] + " " + item["summary"])
    return any(fold(k) in hay for k in keywords)


def run(ctx: Context) -> tuple[int, int]:
    """Fetch all enabled feeds. Returns (requests, new_deals). Skips feeds fetched within min_interval_hours."""
    new = 0
    requests = 0
    for feed in ctx.cfg.feeds:
        url, name = feed["url"], feed.get("name", feed["url"])
        row = ctx.conn.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
        min_h = float(feed.get("min_interval_hours", 6))
        if row and row["fetched_at"]:
            age = ctx.now - dt.datetime.strptime(row["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            if age < dt.timedelta(hours=min_h):
                ctx.log(f"feed {name}: fetched {age} ago, skipping")
                continue
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
        if row and row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row and row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        before = ctx.http.requests
        try:
            status, rh, body = ctx.http.request(url, headers=headers, min_delay=2.0)
            requests += ctx.http.requests - before
            ctx.conn.execute(
                "INSERT OR REPLACE INTO http_cache(url, etag, last_modified, fetched_at) VALUES (?,?,?,?)",
                (url, rh.get("etag"), rh.get("last-modified"), iso(ctx.now)),
            )
            if status == 304:
                continue
            items = parse_feed(body)
        except (HttpError, ET.ParseError) as e:
            requests += ctx.http.requests - before
            ctx.errors += 1
            ctx.log(f"feed {name}: {e}")
            continue
        for it in items:
            if not matches(it, feed.get("keywords", [])):
                continue
            cur = ctx.conn.execute(
                "INSERT OR IGNORE INTO deals(guid, feed, title, url, published, first_seen, summary) VALUES (?,?,?,?,?,?,?)",
                (it["guid"], name, it["title"], it["url"], it["published"], iso(ctx.now), it["summary"]),
            )
            new += cur.rowcount
        ctx.conn.commit()
    return requests, new
