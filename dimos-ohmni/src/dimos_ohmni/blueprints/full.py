"""Ohmni full-stack blueprint: agent + brain + safety + smart.

Composition order matters because of stream remapping:

    OhmniConnection (sensors + drive)
        + WebsocketVisModule (vis on :8765)
        + VoxelGridMapper / CostMapper / ReplanningAStarPlanner / WavefrontFrontierExplorer  (smart)
        + Agent + NavigationSkillContainer + PersonFollowSkillContainer + SpeakSkill + WebInput  (agentic)
        + BrainResearcher  (self-improvement loop, publishes goal_request)
        + SafetyGovernor   (sole writer of cmd_vel; intercepts planner's cmd_vel via remap)

This is the blueprint to run when you want the AI driving the robot
end-to-end: perception → mapping → goal proposal → plan → safety-gated
motion.
"""

import logging

from dimos.core.blueprints import autoconnect

from .agentic import ohmni_agentic

logger = logging.getLogger(__name__)


def _try_import_extras():
    """Try to import brain + safety + semantic-pin. Returns blueprints
    in a dict; keys absent on import failure."""
    out: dict = {}
    try:
        from dimos_ohmni.brain import brain_researcher
        out["brain"] = brain_researcher
    except Exception as e:  # noqa: BLE001
        logger.warning("brain import failed: %s", e)
    try:
        from dimos_ohmni.safety import safety_governor
        out["safety"] = safety_governor
    except Exception as e:  # noqa: BLE001
        logger.warning("safety import failed: %s", e)
    try:
        from dimos_ohmni.perception import semantic_pin
        out["semantic_pin"] = semantic_pin
    except Exception as e:  # noqa: BLE001
        logger.warning("semantic_pin import failed: %s", e)
    try:
        from dimos_ohmni.web_research import web_researcher
        out["web_researcher"] = web_researcher
    except Exception as e:  # noqa: BLE001
        logger.warning("web_researcher import failed: %s", e)
    try:
        from dimos_ohmni.autoresearch import autoresearch_orchestrator
        out["autoresearch"] = autoresearch_orchestrator
    except Exception as e:  # noqa: BLE001
        logger.warning("autoresearch import failed: %s", e)
    return out


_extras = _try_import_extras()
_brain = _extras.get("brain")
_safety = _extras.get("safety")
_semantic_pin = _extras.get("semantic_pin")
_web_researcher = _extras.get("web_researcher")
_autoresearch = _extras.get("autoresearch")

_extra_blueprints = []
if _brain is not None:
    _extra_blueprints.append(_brain())
if _safety is not None:
    _extra_blueprints.append(_safety())
if _semantic_pin is not None:
    _extra_blueprints.append(_semantic_pin())
if _web_researcher is not None:
    _extra_blueprints.append(_web_researcher())
if _autoresearch is not None:
    _extra_blueprints.append(_autoresearch())

if _safety is not None:
    try:
        from dimos.navigation.replanning_a_star.module import (
            ReplanningAStarPlanner,
        )
        from dimos.web.websocket_vis.websocket_vis_module import (
            WebsocketVisModule,
        )

        ohmni_full = (
            autoconnect(ohmni_agentic, *_extra_blueprints)
            .remappings(
                [
                    # Both planner and the web vis module publish
                    # cmd_vel; both must go to raw_cmd_vel so the
                    # SafetyGovernor is the sole writer of /cmd_vel.
                    (ReplanningAStarPlanner, "cmd_vel", "raw_cmd_vel"),
                    (WebsocketVisModule, "cmd_vel", "raw_cmd_vel"),
                ]
            )
            .global_config(n_workers=14, robot_model="ohmni_52")
        )
        logger.info(
            "ohmni_full blueprint live: agent + brain + safety + semantic_pin + smart"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "ohmni_full safety remap path failed: %s — running without safety remap", e
        )
        ohmni_full = (
            autoconnect(ohmni_agentic, *_extra_blueprints)
            .global_config(n_workers=13, robot_model="ohmni_52")
        )
elif _extra_blueprints:
    ohmni_full = (
        autoconnect(ohmni_agentic, *_extra_blueprints)
        .global_config(n_workers=13, robot_model="ohmni_52")
    )
else:
    logger.warning("extras unavailable — falling back to ohmni_agentic")
    ohmni_full = ohmni_agentic
