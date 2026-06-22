"""Agentic Ohmni blueprint: smart + LLM agent with navigation, person follow, speak, web chat.

Mirrors the unitree_go2_agentic stack so the Ohmni gets the same autonomy
primitives:
- LLM-driven planner that can call skills as tools
- Natural-language navigation (NavigateTo / pose tagging via SpatialMemory)
- Person-following via VL detection + visual servoing
- Speak skill (OpenAI TTS through the host audio) — only if
  OPENAI_API_KEY is set; otherwise we use the on-device Android TTS via
  OhmniConnection.say.
- Web chat for human-in-the-loop input

Uses the connection's camera_info_static so person-follow can be constructed
at blueprint time (before the robot connection has started).
"""

import logging
import os

from dimos.core.blueprints import autoconnect

from dimos_ohmni.connection import OhmniConnection

from .smart import ohmni_smart

logger = logging.getLogger(__name__)

try:
    from dimos.agents.skills.navigation import navigation_skill
    from dimos.agents.web_human_input import web_input

    _agentic_modules = [
        navigation_skill(),
        web_input(),
    ]

    # Agent + PersonFollow both depend on a particular langchain/langgraph
    # API shape that is fragile across version drift. PersonFollow
    # additionally requires Agent (via AgentSpec module ref). Gate both
    # behind one env var so a broken Agent import doesn't take down the
    # rest of ohmni_full.
    if os.environ.get("OHMNI_ENABLE_AGENT", "1") == "1":
        try:
            from dimos.agents.agent import agent
            from dimos.agents.skills.person_follow import person_follow_skill
            _agentic_modules.append(agent())
            _agentic_modules.append(
                person_follow_skill(camera_info=OhmniConnection.camera_info_static)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Agent + PersonFollow disabled (langchain/langgraph mismatch): %s. "
                "Set OHMNI_ENABLE_AGENT=0 to silence, or fix the version pin. "
                "Other agentic modules still available.", e,
            )

    # SpeakSkill calls OPENAI_API_KEY at start(), so only include it when
    # the key is set. Otherwise the agent uses OhmniConnection.say (the
    # Android TTS @skill on the connection) which has no external deps.
    if os.environ.get("OPENAI_API_KEY"):
        from dimos.agents.skills.speak_skill import speak_skill
        _agentic_modules.append(speak_skill())
    else:
        logger.warning(
            "OPENAI_API_KEY not set — skipping SpeakSkill (OpenAI TTS). "
            "Agent will use OhmniConnection.say (Android TTS) instead."
        )

    ohmni_agentic = (
        autoconnect(ohmni_smart, *_agentic_modules)
        .global_config(n_workers=10, robot_model="ohmni_52")
    )
except ImportError as e:
    logger.warning(
        "Full agentic blueprint unavailable — missing dependencies: %s. "
        "Falling back to VLM-only agent. Install dimos with agent extras "
        "(langchain, openai, qwen, edge_tam).",
        e,
    )

    try:
        from dimos.agents.vlm_agent import VLMAgent

        ohmni_agentic = (
            autoconnect(ohmni_smart, VLMAgent.blueprint())
            .global_config(n_workers=8, robot_model="ohmni_52")
        )
    except ImportError as e2:
        logger.warning(
            "VLM agentic fallback also unavailable: %s. Using ohmni_smart.", e2
        )
        ohmni_agentic = ohmni_smart
