"""
La Flor Blanca — Labor Alert System
Entry point. Wires up all components and starts the monitor.
"""

import logging

from alert_builder import AlertBuilder
from claude_advisor import ClaudeAdvisor
from config import Config
from daily_briefing import DailyBriefing
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
    monitor.run()
