#!/usr/bin/env python3
"""Run a full autonomous scan: smart blueprint + frontier exploration kicked off automatically.

What this does:
1. Boots the ohmni-smart blueprint (connection + viz + voxel mapper + cost mapper +
   replanning A* + wavefront frontier explorer)
2. Gives the connection ~30s to come up so LiDAR + odom start flowing
3. Calls WavefrontFrontierExplorer.explore() (RPC) to start autonomous frontier
   exploration of unknown space — the explorer handles the wait-for-costmap
   internally
4. Runs until Ctrl+C, then cleanly stops everything

Web UI: http://localhost:8765
"""

import logging
import os
import signal
import sys
import time

# Skip dimos system configurator prompts (multicast route, sysctl, etc.)
os.environ["CI"] = "1"

# Local adb-server on port 5037 was in a stuck state on this host; use 6037
# instead. Bridge subprocesses inherit env so all `adb` calls land on the same
# daemon.
os.environ.setdefault("ANDROID_ADB_SERVER_PORT", "6037")

from dimos.core.global_config import global_config
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


def main() -> None:
    global_config.update(robot_ip="192.168.1.194")

    logger.info("Building ohmni-smart blueprint...")
    from dimos.navigation.frontier_exploration import WavefrontFrontierExplorer
    from dimos_ohmni.blueprints.smart import ohmni_smart

    coordinator = ohmni_smart.build()
    explorer = coordinator.get_instance(WavefrontFrontierExplorer)
    if explorer is None:
        logger.error("Frontier explorer not deployed — aborting.")
        coordinator.stop()
        sys.exit(1)

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        try:
            explorer.stop_exploration()
        except Exception:  # noqa: BLE001
            pass
        coordinator.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Give the bridge / cameras / lidar / mapper time to come up before
    # asking the explorer to plan. The explorer's own loop will keep
    # retrying if the costmap isn't ready yet.
    warmup_s = 30
    logger.info("Warming up subsystems for %ds...", warmup_s)
    time.sleep(warmup_s)

    logger.info("Triggering autonomous frontier exploration...")
    started = explorer.explore()
    if started:
        logger.info("Autonomous scan running. Web UI: http://localhost:8765")
        logger.info("Press Ctrl+C to stop.")
    else:
        logger.warning("explore() returned False (already active?). Continuing.")

    try:
        while True:
            time.sleep(2.0)
            if not explorer.is_exploration_active():
                logger.info("Exploration loop reports inactive — scan complete.")
                break
    except KeyboardInterrupt:
        pass

    shutdown(None, None)


if __name__ == "__main__":
    main()
