# Phase 5 — Safe Free-Roam ("Hermes / OpenClaw" mode)

**Goal:** The LLM has the steering wheel *and we trust it not to drive into a wall*. Agent runs continuously without a human in the loop, but every actuation passes through bounded, sensor-aware safety.

## Hard requirement: SafetyGovernor module

Without this, an LLM with a hallucinated goal can wedge the robot or drive it off a step. **Do not skip.**

```python
class SafetyGovernor(Module):
    """Intercepts cmd_vel from any source. Re-publishes a clamped twist.

    Inputs:
        raw_cmd_vel: In[Twist]      -- agent / planner output (renamed via remap)
        pointcloud:  In[PointCloud2]
        battery:     In[dict]
        odom:        In[PoseStamped]
    Outputs:
        cmd_vel:     Out[Twist]     -- the *only* twist OhmniConnection consumes
        emergency:   Out[bool]      -- latched on hard stop; agent must ack
    """
```

Rules (in order of severity):
1. **Imminent collision** — any lidar return within `0.30 m` in the direction of motion → zero linear, zero angular, latch emergency.
2. **Approaching obstacle** — any return within `0.60 m` ahead → linear capped at `0.10 m/s`.
3. **Speed cap** — global ceiling: `linear_x ∈ [-0.30, +0.30]`, `angular_z ∈ [-1.0, +1.0]` rad/s.
4. **Battery low** — under `15%` → cancel autonomy, route to dock, refuse new goals until charged > `30%`.
5. **Stuck** — `|cmd_vel|` non-zero for ≥`5 s` and odom Δ < `0.05 m` → back up `0.2 m`, log, hand control to human.
6. **Geofence** — if position is > `R` m from a "home" pose, cancel exploration. Default `R = 8 m`; configurable.

## Stream wiring

We need to insert SafetyGovernor *between* the planner and `OhmniConnection`. dimos `autoconnect` matches by stream name + type. To intercept:

```python
# blueprints/free_roam.py
from dimos.core.blueprints import autoconnect, Remap

ohmni_free_roam = autoconnect(
    ohmni_agentic.remap(cmd_vel=Remap(planner="raw_cmd_vel")),
    safety_governor(),  # subscribes raw_cmd_vel, publishes cmd_vel
)
```

(Exact remap syntax to verify against dimos's transport remapping API; this is the intent.)

## Watchdog

Independent of the governor, a `Heartbeat` thread on the host:
- pings `OhmniConnection.battery` every 5 s
- if no response for 30 s, sends `manual_move 0 0` and exits
- if Mac sleeps / network drops, the robot *stops* rather than running on stale commands

## Soft constraints (configurable per session)

```yaml
# ~/.ohmni/safety.yaml
max_linear_mps: 0.30
max_angular_rps: 1.0
imminent_collision_m: 0.30
approach_warning_m: 0.60
geofence_radius_m: 8.0
home_pose: [0.0, 0.0, 0.0]
battery_low_pct: 15
battery_resume_pct: 30
session_timeout_min: 30
allow_skills:
  - move
  - set_neck_angle
  - say
  - observe
  - get_lidar_scan
  - NavigateTo
deny_skills:
  - dock           # too easy to ram the dock — supervise these
  - undock
```

The agent loads this at start; any skill not in `allow_skills` is hidden from its tool list. Updates require human edit to the YAML; agent cannot widen its own permissions.

## Free-roam session loop

1. Human starts session: `dimos run ohmni-free-roam --duration 30m`.
2. Robot says hello, reports battery + position, asks "what would you like me to focus on this session?" (or auto-defaults to "explore").
3. Agent + brain pick goals. Governor enforces constraints. Brain logs.
4. At session timeout OR battery_low OR human "stop", robot returns to dock if mapped, else to home pose, says goodnight, exits.

## Failure modes the governor prevents

- Hallucinated goal in unmapped space → planner refuses; agent re-prompts with a known goal.
- Cliff / step → lidar dome scans floor *just* above floor level, so steps register as voids. Treat unobserved-near-cells as obstacles for forward motion when no recent return below `0.5 m`.
- Person in the way → person-follow's tracker fires before governor; robot already softens approach to keep `1.5 m` standoff.
- Human grabs the robot → odom delta with zero cmd_vel for >2 s ⇒ pause autonomy, ask "should I keep going?".

## Acceptance

- 30-minute unattended session ends with battery > 30%, robot at dock or home pose, brain has ≥10 entries, no human intervention logged.
- Force-fail tests: stand a chair in front of the robot mid-traverse → robot stops, replans around.
- Force-fail: pause Wi-Fi 30 s → host watchdog stops the robot within 30 s.
- Force-fail: ask agent "drive into the wall" — refused (no skill exists; fabricated cmd_vel clamped by governor).

## What this does **not** authorize

- Outdoor operation
- Stairs (geofence keeps it on a single floor; multi-floor mapping is later)
- Operating with passengers / payload (CG and stopping distance change)
- Self-rewriting source code (Phase 4 explicitly excludes this; Phase 5 inherits)
