# Phase 1 — Full Peripheral Skill Surface for the Agent

**Goal:** Every Ohmni capability is a `@skill` an LLM can call by name with documented args. The agent gets the same fluency over the robot that a human teleop operator has over the bot_shell socket.

## Skills to expose on `OhmniConnection`

| skill              | args                                  | maps to                                            | status |
|--------------------|---------------------------------------|----------------------------------------------------|--------|
| `move`             | `linear_x: float, angular_z: float`   | `manual_move` via `OhmniDriveTrain`                | exists, not @skill |
| `set_neck_angle`   | `angle: int 400-600`                  | `neck_angle <val>` via bot_shell                   | exists, not @skill |
| `wake_head`        | —                                     | `wake_head`                                        | exists, not @skill |
| `rest_head`        | —                                     | `rest_head`                                        | exists, not @skill |
| `say`              | `text: str`                           | bot_shell `say <text>` (Android TTS)               | exists, not @skill |
| `set_led`          | `duration: int, hue: int, sat, val`   | bot_shell LED ring                                 | exists, not @skill |
| `set_face`         | `expression: str`                     | HTTP-served SVG on tablet kiosk                    | needs face.py wire |
| `observe`          | —                                     | latest screen-camera frame                         | exists, @skill ✓ |
| `floor_observe`    | —                                     | latest floor-camera frame                          | needs adding |
| `get_battery`      | —                                     | bot_shell `battery`                                | needs adding |
| `get_lidar_scan`   | —                                     | bot_shell `lidar_get_scan` (parsed)                | exists, internal |
| `get_obstacles`    | —                                     | bot_shell `lidar_get_obstacles` (3-zone)           | needs adding |
| `dock`             | —                                     | bot_shell autodock command                         | needs surfacing |
| `undock`           | —                                     | bot_shell undock                                   | needs surfacing |

## Decoration pattern

```python
from dimos.agents.annotation import skill

@skill
def move(self, linear_x: float, angular_z: float) -> str:
    """Drive the robot. linear_x m/s, angular_z rad/s.
    Positive linear_x is forward; positive angular_z is counterclockwise.
    Set both to 0 to stop. Maximum recommended values: linear_x ±0.4,
    angular_z ±1.0.
    """
    ...
```

The docstring is the LLM's only spec, so it has to read like a tool-use schema (units, sign conventions, safe range, side effects).

## System-prompt augmentation

Compose a string of all bot_shell commands and feed it as a non-skill section of the system prompt so the LLM knows the underlying primitives even when no high-level skill exists yet. Source: enumerate `cmd_*` functions on the robot's bot_shell scripts, group by file (motor, neck, lidar, face, autodock).

## Guardrails

- `move` always re-zeroes after `duration` if no follow-up command arrives. Watchdog in `OhmniDriveTrain`.
- `set_neck_angle` clamps to [400, 600].
- `set_led` clamps duration and saturates RGB.

## Acceptance

- `dimos.protocol.rpc list-skills` against the running coordinator shows every entry above as available.
- An LLM can call `move(0.2, 0)` and the robot drives forward; `move(0, 0)` stops it.
- An LLM can ask "what do you see?" and `observe` returns the live frame.
