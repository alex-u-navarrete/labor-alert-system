"""
La Flor Blanca — Labor Alert System
Entry point. Wires up all components and starts the monitor.
"""

import logging

from alert_builder import AlertBuilder
from config import Config
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
    monitor  = LaborMonitor(config, square, notifier, builder)
    monitor.run()
