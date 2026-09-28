"""igepn-mcp: IGEPN (Ecuador) earthquake + volcano reports from its public Telegram channel -> SQLite -> MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import Settings

log = logging.getLogger("igepn_mcp")


async def _poll_loop(settings: Settings) -> None:
    from .poll import poll

    while True:
        try:
            await poll(settings)
        except Exception:  # keep serving even if a poll run blows up
            log.exception("background poll failed")
        await asyncio.sleep(settings.poll_minutes * 60)


async def _serve(settings: Settings, transport: str, host: str, port: int) -> None:
    from .server import BearerAuth, build_server

    mcp = build_server(settings)
    tasks = []
    if settings.poll_minutes > 0:
        tasks.append(asyncio.create_task(_poll_loop(settings)))

    if transport == "stdio":
        await mcp.run_stdio_async()
    else:
        import uvicorn

        if not settings.http_token and host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("serving on %s without IGEPN_MCP_TOKEN - anyone on the network can connect", host)
        app = BearerAuth(mcp.streamable_http_app(host=host), settings.http_token)
        await uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info")).serve()
    for t in tasks:
        t.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(prog="igepn-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll", help="fetch new channel posts once (watermark-incremental; run every ~3 min)")
    bf = sub.add_parser("backfill", help="walk the channel history backwards (resumable, polite)")
    bf.add_argument("--pages", type=int, default=50, help="preview pages to fetch, ~20 posts each (default 50)")
    bf.add_argument("--delay", type=float, default=3.0, help="seconds between pages (default 3)")
    sub.add_parser("reparse", help="rebuild the parsed tables from the raw post log (after a parser change)")
    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # stderr only: stdout is the MCP channel in stdio mode
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()

    if args.cmd == "poll":
        from .poll import poll

        result = asyncio.run(poll(settings))
        sys.exit(0 if result.status == "ok" else 1)
    elif args.cmd == "backfill":
        from .poll import backfill

        asyncio.run(backfill(settings, args.pages, args.delay))
    elif args.cmd == "reparse":
        from .poll import reparse

        print(json.dumps(reparse(settings), indent=1))
    else:
        asyncio.run(_serve(settings, args.transport, args.host, args.port))
