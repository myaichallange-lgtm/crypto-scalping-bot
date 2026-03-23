#!/usr/bin/env python3
"""
launcher.py - Runs both the trading bot and web dashboard in one process.
Used by Docker so a single container handles everything.

Bot engine runs as an asyncio task.
Web server runs as an asyncio task.
Both share the same event loop — no threading needed.
"""

import asyncio
import sys
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot.config import Config
from bot.logger import get_logger
from bot.engine import TradingEngine
from aiohttp import web
import web_server as ws

log = get_logger("launcher", Config.LOG_LEVEL)


async def run_web_server():
    """Start the aiohttp web dashboard."""
    app   = await ws.make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8765)
    await site.start()
    log.info("🌐 Dashboard running at http://0.0.0.0:8765")
    return runner


async def run_bot():
    """Start the trading engine."""
    engine = TradingEngine()
    await engine.start()


async def main():
    log.info("🚀 Launcher starting — bot + dashboard")

    # Run both concurrently
    runner = await run_web_server()
    try:
        await run_bot()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Launcher stopped.")
