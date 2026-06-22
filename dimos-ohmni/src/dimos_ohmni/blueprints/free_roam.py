"""Free-roam Ohmni blueprint: smart + SafetyGovernor.

The governor is the *only* writer of `cmd_vel` to OhmniConnection. The
A* planner's `cmd_vel` is renamed to `raw_cmd_vel`, which the governor
subscribes to, clamps for safety, and republishes as the canonical
`cmd_vel` stream.

This blueprint replaces the planner→connection direct wire with:
    planner.cmd_vel  →  raw_cmd_vel  →  SafetyGovernor.cmd_vel  →  connection
"""

import logging

from dimos.core.blueprints import autoconnect

from .smart import ohmni_smart

logger = logging.getLogger(__name__)

try:
    from dimos.navigation.replanning_a_star.module import ReplanningAStarPlanner
    from dimos_ohmni.safety import safety_governor

    # Remap the planner's cmd_vel output to raw_cmd_vel so the governor
    # can intercept it. SafetyGovernor publishes the actual cmd_vel.
    ohmni_free_roam = (
        autoconnect(ohmni_smart, safety_governor())
        .remappings([(ReplanningAStarPlanner, "cmd_vel", "raw_cmd_vel")])
        .global_config(n_workers=8, robot_model="ohmni_52")
    )
except Exception as e:  # noqa: BLE001
    logger.warning(
        "Free-roam blueprint unavailable: %s. Using ohmni_smart.", e
    )
    ohmni_free_roam = ohmni_smart
