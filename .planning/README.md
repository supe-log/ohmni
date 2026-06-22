# Ohmni autonomy roadmap

Five sequential phases. Each phase has its own file under `phases/`.

| # | Phase | Status | Gates next |
|---|-------|--------|------------|
| 0 | [Autonomous lidar scan](phases/00-autonomous-scan.md) | ✅ Live — frontier goals published (2.00, -0.96), (0.40, 2.82), … 17 valid frontiers detected | Real scan data |
| 1 | [Peripheral skill surface](phases/01-peripheral-skills.md) | ✅ 14 `@skill` methods on OhmniConnection (drive, set_neck_angle, say, set_led, observe, floor_observe, get_battery, get_lidar_scan, get_obstacles, dock, is_docking, …) | Phase 0 |
| 2 | [LLM at the wheel](phases/02-llm-driver.md) | ✅ Stack installed and pinned (langchain 1.0.3, langgraph 1.0.4, langchain-openai 1.0.2). Agent + PersonFollow gated behind OHMNI_ENABLE_AGENT=1 + OPENAI_API_KEY | Phase 1 |
| 3 | [Perception stack](phases/03-perception-stack.md) | ✅ SemanticPin Module live — writes labels to `~/.ohmni/world.json`. VLM hook (`vlm_model="qwen"\|"moondream"`) lazy-built. WebResearcher gives the brain external lookup. | Phase 2 (LLM gates the VLM calls) |
| 4 | [Self-improvement loop](phases/04-autoresearch-loop.md) | ✅ BrainResearcher live — appending to `~/.ohmni/brain.md`. Battery-gated, dock-on-low-battery, revisit-stale + outward-edge proposers running every 60s. | Phase 2 |
| 5 | [Safe free-roam](phases/05-safe-free-roam.md) | ✅ SafetyGovernor live — sole writer of `cmd_vel`, intercepting via `ReplanningAStarPlanner.cmd_vel → raw_cmd_vel` remap. v_max=0.30 m/s, w_max=1.0 rad/s, imminent_collision=0.30 m. | Phase 4 |

## Hardware in scope

- Ohmni 5-2 telepresence robot at `192.168.1.194`
- Two USB cameras (See3CAM_CU135 screen, HD USB floor)
- RPLidar A2M8 front-mounted via cp210x USB-UART (`/dev/ttyUSB0`)
- Drive: differential (left/right wheel), neck servo, LED ring, speaker, Android tablet display
- Compute: Mac host runs dimos + agents; on-device Android runs `telebot_rtc` Node bridge

## Software stack already in place

- `dimos/` — vendored from `dimensionalOS/dimos`, Apache 2.0
- `dimos-ohmni/` — adapter package (Module, blueprints, lidar/camera/drive/audio bridges)
- `control-app/bot_shell_lidar_dimos.js` — patched bot_shell module for lidar (deployed)
- `run_autonomous_scan.py` — Phase 0 entry point

## Component-swap experiment harness (cross-cutting)

When a component isn't working, we want to swap it for an alternate without rewriting the blueprint. Plan:

- All sensor / actuator providers implement a thin Protocol (`LidarProvider`, `DriveProvider`, ...).
- Each provider lives in its own module file. Swapping is a one-line change in the blueprint.
- A `SyntheticLidarProvider` produces fake but plausible scans for dev work without the robot.
- A `RecordedLidarProvider` replays a captured session for regression testing.
- A `SafetyGovernor` (Phase 5) is the only Twist consumer; everything else publishes through it. Swapping the governor (e.g., for a "permissive" dev mode) is one config flag.

This isn't a separate phase; it's the architectural rule for everything we ship.

## Operating principles for the AI driver

- **Brain is local.** No telemetry leaves the host without explicit user opt-in.
- **Skills are typed.** No free-form bot_shell calls from the LLM; only documented `@skill` methods.
- **Governor is mandatory.** Free-roam without Phase 5 is off the table. Always.
- **Compaction over hoarding.** Brain compacts nightly; we keep summaries, not raw transcripts.
- **Reversible by default.** Robot moves are interruptible. Skill side effects (face changes, LED, speech) are non-cumulative.
