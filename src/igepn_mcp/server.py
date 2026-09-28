"""MCP query server: structured, read-only tools over the SQLite IGEPN store."""

from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import Settings
from .db import connect, fold, get_meta, vkey
from .parse import EC_OFFSET
from .telegram import POST_URL

MAX_ITEMS = 50
STALE_AFTER = timedelta(minutes=20)  # poll runs every ~3 min; older than this, say so

INSTRUCTIONS = """\
Ecuador earthquakes and volcano activity from the IGEPN (Instituto Geofísico de la Escuela Politécnica
Nacional, Ecuador's official seismic/volcanic monitoring authority), captured from its public Telegram channel
every few minutes and kept as a growing history. Use it for "what was that tremor / temblor a few minutes ago",
"were there earthquakes in Ecuador today", "how is the Sangay / El Reventador volcano", "any lahar or ash alerts".
It only covers what IGEPN reports (mostly Ecuador and border areas); for worldwide quakes use other tools.

Report ONLY what the data says, and always attribute it to the IGEPN with the date and time. Times are given
in Ecuador local time (`occurred_local_ec`, UTC-5) and UTC. An earthquake `status` of PRELIMINAR means a first
automatic estimate that is usually revised within minutes; REVISADO (older posts: CONFIRMADO) is the reviewed
value - say which one you are giving. Magnitude, depth and location can change a lot between the two.
Volcano reports give two activity levels - `superficial` (surface: emissions, explosions, ash) and `interna`
(internal: seismicity, deformation) - each with a trend (Ascendente / Descendente / Sin cambio). Levels are
IGEPN's own words (Baja, Moderada, Alta, ...); do not translate them into official alert colours. Give the
`url` / `post_url` when the user wants the full report or the image.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _out(obj: dict[str, Any]) -> str:
    """One compact JSON text block (no escaped Spanish accents - tokens matter for a 12B model)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _since(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(then: datetime, now: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 0:
        return "0 min"
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // 1440} d"


def _local(then: datetime) -> str:
    return (then + EC_OFFSET).strftime("%Y-%m-%d %H:%M:%S")


def _clamp(n: int, hi: int = MAX_ITEMS) -> int:
    return min(max(int(n), 1), hi)


def build_server(settings: Settings) -> MCPServer:
    mcp = MCPServer(name="igepn", instructions=INSTRUCTIONS)
    post_url = lambda msg_id: POST_URL.format(channel=settings.channel, msg_id=msg_id)  # noqa: E731

    def db() -> closing[sqlite3.Connection]:
        return closing(connect(settings.db_path))

    def freshness(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        ok = get_meta(conn, "last_poll_ok_utc")
        info: dict[str, Any] = {"last_check_utc": ok}
        if ok is None or now - _parse_utc(ok) > STALE_AFTER:
            info["warning"] = "data may be out of date: the IGEPN channel has not been checked recently"
        return info

    def quake(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
        occurred = _parse_utc(row["occurred_utc"])
        return {
            "evento": row["evento_id"],
            "status": row["status"],
            "occurred_local_ec": _local(occurred),
            "occurred_utc": row["occurred_utc"],
            "ago": _ago(occurred, now),
            "mag": row["mag"],
            "mag_type": row["mag_type"],
            "depth_km": row["depth_km"],
            "lat": row["lat"],
            "lon": row["lon"],
            "place": row["place"],
            "felt_report_url": row["felt_url"],
            "post_url": post_url(row["msg_id"]),
        }

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def last_quake() -> str:
        """The single most recent earthquake reported by the IGEPN, with its latest (revised if available)
        values. Use for "¿qué fue ese temblor?", "what was that earthquake just now", "did it just shake?".

        Check `ago`: if the last quake is hours or days old, the tremor the user felt may not be reported yet
        (a PRELIMINAR report usually appears within ~5 minutes) - say so rather than guessing.
        When the event was revised, `preliminary` shows the first estimate for comparison.
        """
        now = datetime.now(UTC)
        with db() as conn:
            row = conn.execute("SELECT * FROM v_quake_current ORDER BY occurred_utc DESC LIMIT 1").fetchone()
            fresh = freshness(conn, now)
            prelim = None
            if row and row["status"] != "PRELIMINAR":
                prelim = conn.execute(
                    "SELECT * FROM quakes WHERE evento_id=? AND status='PRELIMINAR' ORDER BY msg_id LIMIT 1",
                    (row["evento_id"],),
                ).fetchone()
        if row is None:
            return _out({"found": False, "source": "IGEPN", "data": fresh})
        out = {"found": True, "source": "IGEPN", "quake": quake(row, now), "data": fresh}
        if prelim is not None:
            out["quake"]["preliminary"] = {
                "mag": prelim["mag"], "mag_type": prelim["mag_type"], "depth_km": prelim["depth_km"],
                "place": prelim["place"], "posted_utc": prelim["posted_at"],
            }
        return _out(out)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def latest_quakes(hours: float = 24, min_mag: float | None = None, place: str | None = None, n: int = 15) -> str:
        """Recent earthquakes reported by the IGEPN, newest first, one entry per event (its latest report).
        Use for "earthquakes today / this week", "any tremors near Quito?", "strong quakes this month".

        Args:
            hours: look-back window in hours (default 24; e.g. 168 = one week, 720 = 30 days).
            min_mag: only events with magnitude >= this (e.g. 4).
            place: text that must appear in the location, case/accent-insensitive: a city or province as IGEPN
                writes it, e.g. "Quito", "Manabí", "Esmeraldas" (IGEPN names the NEAREST town, so a quake felt
                in a city may be listed under a neighbouring one).
            n: max events (default 15, max 50).
        """
        now = datetime.now(UTC)
        where, params = ["occurred_utc >= ?"], [_since(hours)]
        if min_mag is not None:
            where.append("mag >= ?")
            params.append(min_mag)
        if place and place.strip():
            where.append("fold(place) LIKE ?")
            params.append(f"%{fold(place.strip())}%")
        sql = f"SELECT * FROM v_quake_current WHERE {' AND '.join(where)} ORDER BY occurred_utc DESC LIMIT ?"
        with db() as conn:
            rows = conn.execute(sql, [*params, _clamp(n)]).fetchall()
            fresh = freshness(conn, now)
        return _out({"source": "IGEPN", "hours": hours, "count": len(rows),
                     "quakes": [quake(r, now) for r in rows], "data": fresh})

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def volcano_status(volcano: str | None = None) -> str:
        """Latest IGEPN activity report for Ecuadorian volcanoes: surface and internal activity level, each
        with its trend. Use for "how is the Sangay", "is Cotopaxi active", "volcano status".

        Without `volcano`: every volcano with a report in the last 30 days (IGEPN only publishes periodic
        reports for volcanoes it is actively following; others are not listed). With `volcano`: that
        volcano's latest report however old (check `report_date`) plus its recent instant alerts.

        Args:
            volcano: name, case/accent/space-insensitive, e.g. "Sangay", "reventador", "Cotopaxi".
        """
        today = (datetime.now(UTC) + EC_OFFSET).date()

        def report(r: sqlite3.Row) -> dict[str, Any]:
            return {
                "volcano": r["volcano"],
                "report": f"Informe {r['report_kind'].capitalize()} N° {r['report_no']}",
                "report_date": r["report_date"],
                "days_old": (today - datetime.strptime(r["report_date"], "%Y-%m-%d").date()).days,
                "superficial": {"level": r["superficial_level"], "trend": r["superficial_trend"]},
                "interna": {"level": r["interna_level"], "trend": r["interna_trend"]},
                "url": r["url"],
                "post_url": post_url(r["msg_id"]),
            }

        latest_sql = """SELECT * FROM volcano_reports v WHERE v.msg_id = (
                            SELECT v2.msg_id FROM volcano_reports v2 WHERE v2.volcano = v.volcano
                            ORDER BY v2.report_date DESC, v2.msg_id DESC LIMIT 1)"""
        with db() as conn:
            if not volcano or not vkey(volcano):
                cutoff = (today - timedelta(days=30)).isoformat()
                rows = conn.execute(f"{latest_sql} AND v.report_date >= ? ORDER BY v.volcano", (cutoff,)).fetchall()
                return _out({"source": "IGEPN", "volcanoes": [report(r) for r in rows]})

            key = f"%{vkey(volcano)}%"
            row = conn.execute(f"{latest_sql} AND vkey(v.volcano) LIKE ? ORDER BY v.report_date DESC LIMIT 1",
                               (key,)).fetchone()
            alerts = conn.execute(
                "SELECT * FROM alerts WHERE vkey(volcano) LIKE ? AND posted_at >= ? ORDER BY posted_at DESC LIMIT 3",
                (key, _since(24 * 7)),
            ).fetchall()
            known = [r["volcano"] for r in conn.execute("SELECT DISTINCT volcano FROM volcano_reports ORDER BY 1")]
        if row is None and not alerts:
            return _out({"source": "IGEPN", "found": False, "volcano": volcano, "volcanoes_with_reports": known})
        out: dict[str, Any] = {"source": "IGEPN", "found": True}
        if row is not None:
            out["status"] = report(row)
        out["alerts_last_7_days"] = [
            {"posted_utc": a["posted_at"], "posted_local_ec": _local(_parse_utc(a["posted_at"])),
             "title": a["title"], "url": a["url"]}
            for a in alerts
        ]
        return _out(out)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def ig_alerts(hours: float = 48, volcano: str | None = None, n: int = 10) -> str:
        """Recent IGEPN instant bulletins (#IGAlInstante: lahars, ash emissions, increased activity) and special
        volcano reports, newest first, as IGEPN wrote them. Use for "any volcano alerts", "is there ash from
        the Sangay", "lahar warnings". Relay safety recommendations exactly as written.

        Args:
            hours: look-back window in hours (default 48; 168 = one week).
            volcano: optional volcano name filter, e.g. "Sangay".
            n: max bulletins (default 10, max 50).
        """
        where, params = ["posted_at >= ?"], [_since(hours)]
        if volcano and vkey(volcano):
            where.append("vkey(volcano) LIKE ?")
            params.append(f"%{vkey(volcano)}%")
        sql = f"SELECT * FROM alerts WHERE {' AND '.join(where)} ORDER BY posted_at DESC LIMIT ?"
        with db() as conn:
            rows = conn.execute(sql, [*params, _clamp(n)]).fetchall()

        def text(t: str, limit: int = 900) -> str:
            return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"

        return _out({"source": "IGEPN", "hours": hours, "count": len(rows), "alerts": [
            {"kind": a["kind"], "volcano": a["volcano"], "posted_utc": a["posted_at"],
             "posted_local_ec": _local(_parse_utc(a["posted_at"])), "title": a["title"], "text": text(a["text"]),
             "url": a["url"], "post_url": post_url(a["msg_id"])}
            for a in rows
        ]})

    return mcp


class BearerAuth:
    """Minimal ASGI gate for streamable-http: requires `Authorization: Bearer <token>` when a token is set.
    /healthz is always open (the Docker healthcheck uses it, token or not)."""

    def __init__(self, app, token: str | None) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            if scope["path"] == "/healthz":
                await _respond(send, 200, b"ok")
                return
            if self.expected is None:
                await self.app(scope, receive, send)
                return
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await _respond(send, 401, b"unauthorized")
                return
        await self.app(scope, receive, send)


async def _respond(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
