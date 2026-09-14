"""Point-in-time crypto news from the public WordPress archives of crypto outlets.

Each post carries its exact publication time (`date_gmt`, UTC), title and excerpt,
so the model gets "title + short summary" items like the challenge data did.
Months are requested in the site's local time and posts are filtered on
`date_gmt`, so nothing published after the decision instant can leak in.
"""
from __future__ import annotations

import bisect
import html
import re
import time
from datetime import UTC, datetime

import httpx

SITES = (
    "www.newsbtc.com", "bitcoinist.com", "cryptopotato.com", "dailyhodl.com",
    "crypto.news", "ambcrypto.com", "bitcoinmagazine.com", "cryptonews.com",
)
ASSET_KEYWORDS = {
    "btc": re.compile(r"\b(?:bitcoin|btc)\b", re.I),
    "eth": re.compile(r"\b(?:ethereum|ether|eth)\b", re.I),
}
_PAGE = 100
_PAUSE_S = 0.5
_SUMMARY_CHARS = 300
_TAGS = re.compile(r"<[^>]+>")
_TRAILERS = re.compile(r"\s*(?:\[\s*(?:…|\.\.\.|&hellip;)\s*\]|…|\.\.\.|Read more\.?)\s*$", re.I)


def _text(rendered: str) -> str:
    text = html.unescape(_TAGS.sub("", rendered))
    return re.sub(r"\s+", " ", text).strip()


def parse_post(site: str, raw: dict) -> dict | None:
    title = _text(raw.get("title", {}).get("rendered", ""))
    if not title or not raw.get("date_gmt"):
        return None
    summary = _TRAILERS.sub("", _text(raw.get("excerpt", {}).get("rendered", "")))
    seen = int(datetime.fromisoformat(raw["date_gmt"]).replace(tzinfo=UTC).timestamp())
    return {"site": site, "seen": seen, "title": title, "summary": summary[:_SUMMARY_CHARS]}


def fetch_month(client: httpx.Client, site: str, year: int, month: int) -> list[dict]:
    """All posts a site published in one (site-local) calendar month."""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    posts: list[dict] = []
    page = 1
    while True:
        for attempt in range(6):
            resp = client.get(f"https://{site}/wp-json/wp/v2/posts", params={
                "after": f"{year}-{month:02d}-01T00:00:00", "before": f"{ny}-{nm:02d}-01T00:00:00",
                "per_page": _PAGE, "page": page, "orderby": "date", "order": "asc",
                "_fields": "date_gmt,title,excerpt",
            })
            if resp.status_code not in (429, 502, 503, 504):
                break
            time.sleep(10 * (attempt + 1))
        if resp.status_code == 400 and page > 1:
            break
        resp.raise_for_status()
        posts.extend(p for p in (parse_post(site, raw) for raw in resp.json()) if p)
        if page >= int(resp.headers.get("x-wp-totalpages", "1")):
            break
        page += 1
        time.sleep(_PAUSE_S)
    return posts


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


class Archive:
    """Posts indexed by publication time."""

    def __init__(self, posts: list[dict]):
        self.posts = sorted(posts, key=lambda p: p["seen"])
        self._seen = [p["seen"] for p in self.posts]

    def between(self, start_s: int, end_s: int) -> list[dict]:
        """Posts with start_s <= seen < end_s, newest first."""
        lo = bisect.bisect_left(self._seen, start_s)
        hi = bisect.bisect_left(self._seen, end_s)
        return self.posts[lo:hi][::-1]


def select_news(archive: Archive, asset: str, t_dec: int, k: int,
                window_s: int = 86_400) -> list[dict]:
    """Newest-first unique posts from the 24h before `t_dec`.

    Posts that mention the asset come first; general crypto posts fill any gap.
    """
    in_window = archive.between(t_dec - window_s, t_dec)
    keyword = ASSET_KEYWORDS[asset]
    mentions = [bool(keyword.search(p["title"]) or keyword.search(p["summary"])) for p in in_window]
    on_asset = [p for p, hit in zip(in_window, mentions) if hit]
    general = [p for p, hit in zip(in_window, mentions) if not hit]
    picked: list[dict] = []
    titles: set[str] = set()
    for post in on_asset + general:
        key = _norm(post["title"])
        if key in titles:
            continue
        titles.add(key)
        picked.append(post)
        if len(picked) == k:
            break
    return picked
