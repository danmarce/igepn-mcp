"""Poller: fetch preview page(s) -> classify -> append raw -> derive parsed rows. Watermark-incremental.

Two keys, two jobs: the Telegram message id (watermark) says what's NEW; the IGEPN `Evento` id says what's
CORRECTED - a [REVISADO] arrives as a new message carrying the same Evento, and `v_quake_current` picks it up.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from .config import Settings
from .db import connect, get_meta, set_meta
from .parse import ALERT, DROPPED, QUAKE, VOLCANO_REPORT, classify, parse_alert, parse_quake, parse_volcano_report
from .telegram import Post, fetch_page, make_client

log = logging.getLogger("igepn_mcp.poll")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive(conn: sqlite3.Connection, msg_id: int, posted_at: str, text: str, cls: str) -> bool:
    """Write the parsed row for one stored post. Returns False if a structured post failed to parse."""
    if cls == QUAKE:
        q = parse_quake(text)
        if q is None:
            return False
        conn.execute(
            """INSERT OR REPLACE INTO quakes (msg_id, evento_id, status, occurred_utc, mag, mag_type, depth_km,
                                              lat, lon, place, felt_url, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, q.evento_id, q.status, q.occurred_utc, q.mag, q.mag_type, q.depth_km, q.lat, q.lon,
             q.place, q.felt_url, posted_at),
        )
    elif cls == VOLCANO_REPORT:
        r = parse_volcano_report(text, posted_at)
        if r is None:
            return False
        conn.execute(
            """INSERT OR REPLACE INTO volcano_reports (msg_id, volcano, report_kind, report_no, report_date,
                   superficial_level, superficial_trend, interna_level, interna_trend, url, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, r.volcano, r.report_kind, r.report_no, r.report_date, r.superficial_level,
             r.superficial_trend, r.interna_level, r.interna_trend, r.url, posted_at),
        )
    elif cls == ALERT:
        a = parse_alert(text)
        conn.execute(
            """INSERT OR REPLACE INTO alerts (msg_id, kind, volcano, title, text, url, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, a.kind, a.volcano, a.title, a.text, a.url, posted_at),
        )
    return True


@dataclass
class StoreStats:
    new: int = 0
    dropped: int = 0
    unparsed: list[int] = field(default_factory=list)
    by_class: dict[str, int] = field(default_factory=dict)


def store_posts(conn: sqlite3.Connection, posts: list[Post], stats: StoreStats) -> None:
    """Append posts not seen before (INSERT OR IGNORE: the raw log is never overwritten)."""
    fetched = now_iso()
    for p in posts:
        cls = classify(p.text)
        if cls in DROPPED:
            stats.dropped += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO posts_raw (msg_id, posted_at, class, raw_text, media_url, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (p.msg_id, p.posted_at, cls, p.text, p.media_url, fetched),
        )
        if not cur.rowcount:
            continue
        stats.new += 1
        stats.by_class[cls] = stats.by_class.get(cls, 0) + 1
        if not derive(conn, p.msg_id, p.posted_at, p.text, cls):
            stats.unparsed.append(p.msg_id)
            log.warning("post %d classified %s but did not parse; kept raw (fix the parser, then `reparse`)",
                        p.msg_id, cls)
    conn.commit()


@dataclass
class PollResult:
    status: str  # "ok" | "error: ..."
    pages: int = 0
    stats: StoreStats = field(default_factory=StoreStats)
    watermark: int | None = None


async def poll(settings: Settings) -> PollResult:
    """Fetch the newest page; page back with ?before= only while there's a gap above the watermark.
    A failed fetch is recorded and skipped - never fatal."""
    conn = connect(settings.db_path)
    result = PollResult(status="ok")
    try:
        wm_raw = get_meta(conn, "watermark")
        watermark = int(wm_raw) if wm_raw else None
        fresh: list[Post] = []
        try:
            async with make_client(settings.user_agent, settings.fetch_timeout) as client:
                before = None
                while True:
                    page = await fetch_page(client, settings.channel, before)
                    result.pages += 1
                    fresh += [p for p in page if watermark is None or p.msg_id > watermark]
                    # First run: just the newest page (`backfill` walks history). Otherwise stop at the watermark.
                    if not page or watermark is None or page[0].msg_id <= watermark + 1:
                        break
                    if result.pages >= settings.max_catchup_pages:
                        log.warning("catch-up cap hit (%d pages); posts below %d and above watermark %d are "
                                    "missed - run `backfill` to fill the gap", result.pages, page[0].msg_id, watermark)
                        break
                    before = page[0].msg_id
        except (httpx.HTTPError, OSError) as exc:
            result.status = f"error: {type(exc).__name__}: {exc}"[:300]

        store_posts(conn, fresh, result.stats)  # whatever was fetched before an error still counts
        if fresh:
            watermark = max(watermark or 0, *(p.msg_id for p in fresh))
            set_meta(conn, "watermark", watermark)
            oldest = get_meta(conn, "oldest_seen")
            set_meta(conn, "oldest_seen", min(int(oldest) if oldest else fresh[0].msg_id, *(p.msg_id for p in fresh)))
        result.watermark = watermark
        now = now_iso()
        set_meta(conn, "last_poll_utc", now)
        set_meta(conn, "last_poll_status", result.status)
        if result.status == "ok":
            set_meta(conn, "last_poll_ok_utc", now)
        conn.commit()
        s = result.stats
        log.info("poll %s: %d page(s), %d new %s, %d dropped, watermark %s",
                 result.status, result.pages, s.new, s.by_class or "", s.dropped, watermark)
        return result
    finally:
        conn.close()


async def backfill(settings: Settings, pages: int, delay: float = 3.0) -> int:
    """Walk the channel's history backwards from the oldest post seen, `pages` preview pages (~20 posts each).
    Resumable: the cursor is stored, so repeated runs continue where the last one stopped. Returns posts added."""
    conn = connect(settings.db_path)
    try:
        if get_meta(conn, "watermark") is None:
            conn.close()
            await poll(settings)  # establish the live watermark first so poll and backfill never leave a gap
            conn = connect(settings.db_path)
        stats = StoreStats()
        async with make_client(settings.user_agent, settings.fetch_timeout) as client:
            for i in range(pages):
                cursor = get_meta(conn, "oldest_seen")
                if cursor is None or int(cursor) <= 1:
                    log.info("backfill: reached the start of the channel")
                    break
                try:
                    page = await fetch_page(client, settings.channel, int(cursor))
                except (httpx.HTTPError, OSError) as exc:
                    log.warning("backfill stopped at before=%s: %s", cursor, exc)
                    break
                if not page:
                    set_meta(conn, "oldest_seen", 1)
                    conn.commit()
                    log.info("backfill: reached the start of the channel")
                    break
                store_posts(conn, page, stats)
                set_meta(conn, "oldest_seen", page[0].msg_id)
                conn.commit()
                log.info("backfill page %d/%d: msg %d-%d (%s), %d new so far", i + 1, pages, page[0].msg_id,
                         page[-1].msg_id, page[0].posted_at[:10], stats.new)
                await asyncio.sleep(delay)  # polite: one page every few seconds
        log.info("backfill done: %d new %s, %d dropped, %d unparsed %s", stats.new, stats.by_class, stats.dropped,
                 len(stats.unparsed), stats.unparsed[:20])
        return stats.new
    finally:
        conn.close()


def reparse(settings: Settings) -> dict[str, int]:
    """Rebuild every derived table from `posts_raw` (after a parser fix). The raw log is untouched,
    except that each post's `class` is recomputed."""
    conn = connect(settings.db_path)
    try:
        conn.execute("DELETE FROM quakes")
        conn.execute("DELETE FROM volcano_reports")
        conn.execute("DELETE FROM alerts")
        counts: dict[str, int] = {}
        for row in conn.execute("SELECT msg_id, posted_at, raw_text FROM posts_raw ORDER BY msg_id").fetchall():
            cls = classify(row["raw_text"])
            conn.execute("UPDATE posts_raw SET class=? WHERE msg_id=?", (cls, row["msg_id"]))
            ok = derive(conn, row["msg_id"], row["posted_at"], row["raw_text"], cls)
            key = cls if ok else f"{cls}_unparsed"
            counts[key] = counts.get(key, 0) + 1
        conn.commit()
        return counts
    finally:
        conn.close()
