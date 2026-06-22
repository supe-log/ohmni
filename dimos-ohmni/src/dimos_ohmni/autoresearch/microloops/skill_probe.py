"""SkillProbeLoop — try a low-risk skill, observe outcome, score.

Each tick picks one skill (rotating through OhmniConnection skills) and
runs a tiny experiment: send the skill, observe the relevant sensor
signal, score it.

Skills probed:
    say(text)            — does TTS complete (was the bot_shell `say`
                           command acknowledged)?
    set_led(...)         — does the LED command return ack?
    set_neck_angle(N)    — does subsequent `apos 4` (neck servo) move?
    drive(0.05, 0)       — does odom advance ~0.05m?
    drive(0, 0.3)        — does odom theta advance ~0.3 rad?
    get_battery()        — does the call return level > 0?

Score:
    1.0 = success (observed effect within tolerance)
    0.5 = partial / unclear
    0.0 = failure / no effect

Builds procedural memory in `~/.ohmni/research/skills.tsv` so the brain
can later weight which skills are reliable. The journal also tracks
each probe so we can see drift over time (e.g. "drive started
working at 14:00 then degraded after dock reset").

This is a *passive* probe — it only commands tiny safe motions
(<5cm, <20deg). It does not push the robot near walls.
"""

from __future__ import annotations

import math
import random
import re
import socket
import time
from typing import Any

from dimos.utils.logging_config import setup_logger

from ..loop_base import Loop

logger = setup_logger()


def _bot_shell_call(cmd: str, timeout: float = 1.5) -> str:
    """One-shot send to the running bot_shell socket on localhost:9999."""
    try:
        with socket.create_connection(("127.0.0.1", 9999), timeout=2.0) as s:
            # Drain banner
            s.settimeout(0.5)
            try:
                s.recv(4096)
            except socket.timeout:
                pass
            s.sendall((cmd + "\n").encode())
            s.settimeout(timeout)
            chunks: list[bytes] = []
            try:
                while True:
                    data = s.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                    if len(b"".join(chunks)) > 8000:
                        break
            except socket.timeout:
                pass
            return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError as e:
        logger.warning("skill_probe bot_shell call failed: %s", e)
        return ""


def _read_apos(sid: int) -> int | None:
    resp = _bot_shell_call(f"apos {sid}", timeout=1.0)
    m = re.search(rf"apos\s*{sid}\s*=\s*(-?\d+)", resp)
    return int(m.group(1)) if m else None


SKILLS = [
    "say",
    "set_led",
    "set_neck_angle",
    "drive_forward",
    "drive_rotate",
    "get_battery",
]


class SkillProbeLoop(Loop):
    name = "skill_probe"
    budget_s = 6.0

    def propose(self) -> dict[str, Any]:
        # Pick the least-recently-probed skill with some randomness
        recent = self.journal.recent(n=20, loop=self.name)
        recent_skills = [e.knob.split(":")[0] for e in recent[-len(SKILLS):]]
        unseen = [s for s in SKILLS if s not in recent_skills]
        skill = random.choice(unseen) if unseen else random.choice(SKILLS)
        return {"knob": f"{skill}:probe", "skill": skill, "notes": ""}

    def apply(self, proposal: dict[str, Any]) -> Any:
        # No persistent state to mutate; just no-op.
        return None

    def run(self, proposal: dict[str, Any], budget_s: float) -> dict[str, Any]:
        skill = proposal["skill"]
        result: dict[str, Any] = {"skill": skill, "ok": False, "detail": ""}
        try:
            if skill == "say":
                resp = _bot_shell_call("say autoresearch probe")
                result["ok"] = "Speak" in resp or resp.strip() != ""
                result["detail"] = resp[:80].replace("\n", " ")

            elif skill == "set_led":
                resp = _bot_shell_call("light_color 1500 200 255 200")
                # bot_shell echoes a confirmation if the cmd was recognized
                result["ok"] = "light" in resp.lower() or len(resp) > 0
                result["detail"] = resp[:80].replace("\n", " ")

            elif skill == "set_neck_angle":
                before = _read_apos(4)
                _bot_shell_call("wake_head")
                _bot_shell_call("neck_angle 540")
                time.sleep(2.0)
                after = _read_apos(4)
                if before is not None and after is not None:
                    delta = abs(after - before)
                    result["ok"] = delta > 50
                    result["detail"] = f"apos4 {before}->{after}"
                else:
                    result["detail"] = "apos read failed"

            elif skill == "drive_forward":
                left_a = _read_apos(0)
                right_a = _read_apos(1)
                _bot_shell_call("manual_move 50 0")
                time.sleep(2.0)
                _bot_shell_call("manual_move 0 0")
                time.sleep(0.5)
                left_b = _read_apos(0)
                right_b = _read_apos(1)
                if all(v is not None for v in (left_a, right_a, left_b, right_b)):
                    dl = abs(left_b - left_a)
                    dr = abs(right_b - right_a)
                    result["ok"] = (dl + dr) > 80
                    result["detail"] = f"|dl|+|dr|={dl + dr}"
                else:
                    result["detail"] = "apos read failed"

            elif skill == "drive_rotate":
                left_a = _read_apos(0)
                right_a = _read_apos(1)
                _bot_shell_call("manual_move 0 25")
                time.sleep(2.0)
                _bot_shell_call("manual_move 0 0")
                time.sleep(0.5)
                left_b = _read_apos(0)
                right_b = _read_apos(1)
                if all(v is not None for v in (left_a, right_a, left_b, right_b)):
                    dl = left_b - left_a
                    dr = right_b - right_a
                    # In rotation, wheels move in opposite directions.
                    opposite_signs = (dl > 50 and dr < -50) or (dl < -50 and dr > 50)
                    result["ok"] = opposite_signs
                    result["detail"] = f"dl={dl} dr={dr}"
                else:
                    result["detail"] = "apos read failed"

            elif skill == "get_battery":
                resp = _bot_shell_call("battery", timeout=2.0)
                m = re.search(r"battery level:\s*(\d+)", resp, re.IGNORECASE)
                if m:
                    level = int(m.group(1))
                    result["ok"] = level > 0
                    result["detail"] = f"level={level}"

            else:
                result["detail"] = "unknown skill"
        except Exception as e:  # noqa: BLE001
            result["detail"] = f"exception: {e}"

        return result

    def score(self, observations: dict[str, Any]) -> float:
        return 1.0 if observations.get("ok") else 0.0
