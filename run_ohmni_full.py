#!/usr/bin/env python3
"""Run the Ohmni full stack — agent + brain + safety + semantic-pin + smart.

This is the maximum-autonomy entry point. It boots:
- OhmniConnection (sensors + drive)
- WebsocketVisModule (vis on :8765)
- VoxelGridMapper / CostMapper / ReplanningAStarPlanner / WavefrontFrontierExplorer (smart)
- Agent + NavigationSkillContainer + PersonFollowSkillContainer + SpeakSkill + WebInput
- BrainResearcher (~/.ohmni/brain.md)
- SafetyGovernor (clamps every cmd_vel before it reaches the drive)
- SemanticPin (~/.ohmni/world.json — labels at poses)

Web UI:
    chat / control:        http://localhost:8765
    websocket vis:         ws://localhost:7779

Required env:
    ANDROID_ADB_SERVER_PORT=6037   (set automatically below)
    OPENAI_API_KEY=sk-...          (or another provider in dimos.agents)

Stop with Ctrl+C — the SafetyGovernor's stop() guarantees a final
zero-twist before tear-down.
"""

import logging
import os
import signal
import sys
import time

os.environ["CI"] = "1"
os.environ.setdefault("ANDROID_ADB_SERVER_PORT", "6037")

from dimos.core.global_config import global_config
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


def main() -> None:
    global_config.update(robot_ip="192.168.1.194")
    if not os.environ.get("OPENAI_API_KEY"):
        logger.warning(
            "OPENAI_API_KEY not set — Agent + SpeakSkill will fail at first call. "
            "Set it in env before running for the LLM-driven session."
        )

    logger.info("Building ohmni_full blueprint...")
    from dimos_ohmni.blueprints.full import ohmni_full

    coordinator = ohmni_full.build()

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        coordinator.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("ohmni_full live. Web chat: http://localhost:8765 — Ctrl+C to stop.")

    # Auto-trigger frontier exploration if requested.
    if os.environ.get("OHMNI_AUTO_EXPLORE", "1") == "1":
        from dimos.navigation.frontier_exploration import WavefrontFrontierExplorer
        explorer = coordinator.get_instance(WavefrontFrontierExplorer)
        if explorer is not None:
            logger.info("Warming up 30s before triggering frontier exploration...")
            time.sleep(30)
            try:
                started = explorer.explore()
                logger.info(
                    "Frontier explore() -> %s. Brain will also propose every "
                    "%ss when battery is healthy.",
                    started, 60,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("explore() failed: %s", e)
        else:
            logger.warning("WavefrontFrontierExplorer not deployed — skipping auto-explore")
    try:
        while True:
            time.sleep(2.0)
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
