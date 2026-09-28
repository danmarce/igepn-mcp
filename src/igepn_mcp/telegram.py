"""Transport: the channel's public `t.me/s/<channel>` preview page (keyless, served to any honest client).

Each page holds ~20 posts, oldest first; `?before=<msg_id>` pages back, and it reaches the channel's first post.
"""

from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

import httpx
import truststore

PREVIEW_URL = "https://t.me/s/{channel}"
POST_URL = "https://t.me/{channel}/{msg_id}"


@dataclass(frozen=True)
class Post:
    msg_id: int
    posted_at: str  # ISO-8601 UTC "YYYY-MM-DDTHH:MM:SSZ"
    text: str  # plain text, line breaks kept (the formats are line-oriented)
    media_url: str | None = None


def _iso_utc(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _PreviewParser(HTMLParser):
    """Collects posts from a preview page. Only the post's own text div (`js-message_text`) is read -
    never a quoted reply (`js-message_reply_text`) or a link preview."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[Post] = []
        self._cur: dict | None = None
        self._text_depth = 0  # >0 while inside the text div (counts nested divs)

    def _flush(self) -> None:
        c = self._cur
        if c and c["posted_at"]:
            lines = [ln.strip() for ln in "".join(c["text"]).split("\n")]
            text = "\n".join(lines).strip()
            text = re.sub(r"\n{3,}", "\n\n", text)
            self.posts.append(Post(c["msg_id"], c["posted_at"], text, c["media"]))
        self._cur = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div" and (post := a.get("data-post")) and "js-widget_message" in cls:
            self._flush()
            msg_id = post.rsplit("/", 1)[-1]
            self._cur = {"msg_id": int(msg_id), "posted_at": None, "text": [], "media": None} if msg_id.isdigit() else None
            return
        c = self._cur
        if c is None:
            return
        if self._text_depth:
            if tag == "div":
                self._text_depth += 1
            elif tag == "br":
                c["text"].append("\n")
            return
        if tag == "div" and "js-message_text" in cls.split():
            self._text_depth = 1
        elif tag == "time" and a.get("datetime") and not c["posted_at"]:
            c["posted_at"] = _iso_utc(a["datetime"])
        elif tag == "a" and "tgme_widget_message_photo_wrap" in cls and not c["media"]:
            if m := re.search(r"background-image:url\('([^']+)'\)", a.get("style") or ""):
                c["media"] = m.group(1)

    def handle_endtag(self, tag: str) -> None:
        if self._text_depth and tag == "div":
            self._text_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._text_depth and self._cur is not None:
            self._cur["text"].append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def parse_preview(html: str) -> list[Post]:
    """Posts on one preview page, sorted by message id (oldest first)."""
    p = _PreviewParser()
    p.feed(html)
    p.close()
    return sorted({post.msg_id: post for post in p.posts}.values(), key=lambda post: post.msg_id)


def make_client(user_agent: str, timeout: float) -> httpx.AsyncClient:
    # truststore: verify TLS against the OS trust store (a plain CA bundle in the container; also works on a dev
    # machine behind a TLS-inspecting proxy such as Netskope, where certifi's bundle is rejected).
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent, "Accept": "text/html"},
        timeout=timeout,
        follow_redirects=True,
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )


async def fetch_page(client: httpx.AsyncClient, channel: str, before: int | None = None) -> list[Post]:
    """One preview page (newest posts, or those just below `before`). Raises httpx errors on failure."""
    resp = await client.get(PREVIEW_URL.format(channel=channel), params={"before": before} if before else None)
    resp.raise_for_status()
    return parse_preview(resp.text)
