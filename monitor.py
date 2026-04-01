"""
La Flor Blanca — Labor Alert System
Entry point. Wires up all components, starts the background scheduler,
then starts the FastAPI web dashboard (blocks main thread for Railway).
"""

import logging
import os

import uvicorn

from alert_builder import AlertBuilder
from claude_advisor import ClaudeAdvisor
from config import Config
from daily_briefing import DailyBriefing
from dashboard import DashboardApp
from notifier import Notifier
from scheduler import LaborMonitor
from square_client import SquareDataClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

if __name__ == "__main__":
    config   = Config()
    square   = SquareDataClient(config)
    notifier = Notifier(config)
    builder  = AlertBuilder(config)
    advisor  = ClaudeAdvisor(config) if config.claude_enabled else None
    briefing = DailyBriefing(config, square, notifier) if config.claude_enabled else None
    monitor  = LaborMonitor(config, square, notifier, builder, advisor, briefing)

    # Start scheduler in background threads (non-blocking)
    monitor.start()

    # Start web dashboard — blocks main thread, keeping the Railway process alive
    dash = DashboardApp(config, square, monitor)
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(dash.app, host="0.0.0.0", port=port, log_level="warning")
