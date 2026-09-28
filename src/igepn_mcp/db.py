"""SQLite storage: append-only raw log + derived (re-buildable) parsed tables + the current-quake view.

ACCUMULATE, never prune: this is a growing geophysical catalog. `posts_raw` is the golden record; the parsed
tables are derived from it and can be rebuilt at any time (`igepn-mcp reparse`) when the parser improves.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path

SCHEMA = """
-- Every kept post exactly as captured. Immutable (only `class` may be recomputed by reparse).
CREATE TABLE IF NOT EXISTS posts_raw (
    msg_id     INTEGER PRIMARY KEY,   -- Telegram message id (data-post="<channel>/<id>")
    posted_at  TEXT NOT NULL,         -- ISO-8601 UTC, from the post's <time datetime>
    class      TEXT NOT NULL,         -- quake | volcano_report | alert | outreach | other
    raw_text   TEXT NOT NULL,
    media_url  TEXT,                  -- first photo (Telegram CDN; may expire - post_url is the stable link)
    fetched_at TEXT NOT NULL
);

-- One row per earthquake REPORT: preliminary and revised versions all survive (revision history is a dataset).
CREATE TABLE IF NOT EXISTS quakes (
    msg_id       INTEGER PRIMARY KEY REFERENCES posts_raw (msg_id),
    evento_id    TEXT NOT NULL,
    status       TEXT NOT NULL,       -- PRELIMINAR | REVISADO | CONFIRMADO (older name for REVISADO)
    occurred_utc TEXT NOT NULL,       -- source gives local time (UTC-5); normalized here
    mag          REAL,
    mag_type     TEXT,
    depth_km     REAL,
    lat          REAL,
    lon          REAL,
    place        TEXT,
    felt_url     TEXT,
    posted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quakes_evento ON quakes (evento_id, msg_id);
CREATE INDEX IF NOT EXISTS quakes_occurred ON quakes (occurred_utc DESC);

-- Current value per event: a revised report wins over the preliminary; among equals, the newest message.
CREATE VIEW IF NOT EXISTS v_quake_current AS
SELECT q.*,
       (SELECT COUNT(*) FROM quakes r WHERE r.evento_id = q.evento_id) AS n_reports
FROM quakes q
WHERE q.msg_id = (
    SELECT q2.msg_id FROM quakes q2 WHERE q2.evento_id = q.evento_id
    ORDER BY (q2.status = 'PRELIMINAR') ASC, q2.msg_id DESC LIMIT 1
);

-- Periodic volcano reports (Informe Diario / Semanal / Mensual). History kept for trends.
CREATE TABLE IF NOT EXISTS volcano_reports (
    msg_id            INTEGER PRIMARY KEY REFERENCES posts_raw (msg_id),
    volcano           TEXT NOT NULL,  -- display name, e.g. "El Reventador"
    report_kind       TEXT NOT NULL,  -- diario | semanal | mensual
    report_no         TEXT,
    report_date       TEXT NOT NULL,  -- YYYY-MM-DD (local date stated in the report)
    superficial_level TEXT,
    superficial_trend TEXT,
    interna_level     TEXT,
    interna_trend     TEXT,
    url               TEXT,
    posted_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS volcano_reports_v ON volcano_reports (volcano, report_date DESC);

-- Free-text bulletins: #IGAlInstante (lahar / ash / activity) and INFORME VOLCANICO ESPECIAL.
CREATE TABLE IF NOT EXISTS alerts (
    msg_id    INTEGER PRIMARY KEY REFERENCES posts_raw (msg_id),
    kind      TEXT NOT NULL,          -- instante | especial
    volcano   TEXT,
    title     TEXT NOT NULL,
    text      TEXT NOT NULL,
    url       TEXT,
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_posted ON alerts (posted_at DESC);

-- Poller state: watermark (max message id seen), backfill cursor, last poll health.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def fold(text: str | None) -> str:
    """Case- and accent-insensitive form: 'Manabí' -> 'manabi'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def vkey(name: str | None) -> str:
    """Volcano match key: 'El Reventador', '#ElReventador' and 'EL REVENTADOR' all -> 'elreventador'."""
    return re.sub(r"[^a-z0-9]", "", fold(name))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # the poller writes while the server reads
    conn.execute("PRAGMA busy_timeout=30000")
    conn.create_function("fold", 1, fold, deterministic=True)
    conn.create_function("vkey", 1, vkey, deterministic=True)
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | int | None) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, None if value is None else str(value)),
    )
