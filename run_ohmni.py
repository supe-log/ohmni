#!/usr/bin/env python3
"""Direct launcher for dimos + Ohmni — bypasses CLI system checks."""

import os
import signal
import sys
import time

# Skip dimos system configurator prompts (multicast route, sysctl, etc.)
os.environ["CI"] = "1"

from dimos.core.blueprints import autoconnect
from dimos.core.global_config import global_config
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


def main():
    # Configure global settings
    global_config.update(robot_ip="192.168.1.194")

    logger.info("Loading ohmni-smart blueprint (SLAM + navigation + exploration)...")
    from dimos_ohmni.blueprints.smart import ohmni_smart

    logger.info("Building blueprint...")
    coordinator = ohmni_smart.build()

    # Graceful shutdown on Ctrl+C
    def shutdown(sig, frame):
        logger.info("Shutting down...")
        coordinator.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("dimos + Ohmni running!")
    logger.info("Web UI: http://localhost:8765")
    logger.info("Press Ctrl+C to stop")

    # Keep alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        coordinator.stop()


if __name__ == "__main__":
    main()
