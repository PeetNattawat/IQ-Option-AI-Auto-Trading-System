"""
HTTP server for iqoption-ai dashboard.
Serves dashboard.html at GET / and any other static files in the project directory.
Runs on port 8766 (default, override with env var DASHBOARD_HTTP_PORT).

Start alongside the WS server in main.py via asyncio.gather():
    from http_server import run_http_server
    await asyncio.gather(ws_server_full(), run_http_server(), ...)

Or run standalone for testing:
    python http_server.py
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

# Root of the project (same directory as this file)
BASE_DIR = Path(__file__).parent
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
HTTP_PORT = int(os.getenv("DASHBOARD_HTTP_PORT", "8766"))
HTTP_HOST = "127.0.0.1"


async def handle_root(request: web.Request) -> web.Response:
    """Serve dashboard.html at GET /"""
    if not DASHBOARD_FILE.exists():
        return web.Response(status=404, text="dashboard.html not found")
    content = DASHBOARD_FILE.read_bytes()
    return web.Response(
        body=content,
        content_type="text/html",
        charset="utf-8",
    )


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint — used by Cloudflare Tunnel and monitoring."""
    payload = json.dumps({"status": "ok", "bot": "running", "ts": int(time.time())})
    return web.Response(
        text=payload,
        content_type="application/json",
    )


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    # NOTE: handle_static route removed (Iris QA bug-036) — it exposed .env and all project
    # files via GET /{path}. dashboard.html is fully self-contained and served by handle_root.
    # If future static assets (.js/.css) are needed, add an allowlist-based handler instead.
    return app


async def run_http_server():
    """
    Async coroutine — plug into asyncio.gather() in main.py.
    Runs until cancelled (i.e. for the lifetime of the process).
    """
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()
    logger.info(f"[HTTP] Dashboard server running at http://{HTTP_HOST}:{HTTP_PORT}")
    print(f"[HTTP] Dashboard at http://{HTTP_HOST}:{HTTP_PORT} — open in browser")
    try:
        await asyncio.Future()  # run forever (same pattern as ws_server_full)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(run_http_server())
