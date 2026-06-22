# Phase 0 — Autonomous Lidar Scan (in flight)

**Goal:** Robot autonomously maps the room with frontier exploration; LLM not required.

**Status:** Stack complete; physical lidar motor needs to spin.

## What was built this session

- `dimos-ohmni/src/dimos_ohmni/connection.py`
  - Added `OhmniConnection.camera_info_static` (320×240 See3CAM intrinsics) so skills like person-follow can read it at blueprint construction time.
  - Added `_publish_odom_loop` thread (5 Hz). Earlier the explorer deadlocked because dead-reckoned odom only updated inside `move()`; it now publishes continuously so planner / explorer have a pose to plan against from t=0.
  - Switched all loggers to `dimos.utils.logging_config.setup_logger()` so worker-process logs propagate.
  - Periodic `lidar_scan_count` log every 25 frames in `_on_lidar_scan`.

- `dimos-ohmni/src/dimos_ohmni/lidar.py`
  - Replaced raw-serial reader with a bot_shell poller (`lidar_get_scan` polls at ~6 Hz for one revolution at a time). Avoids fighting the on-device collision-detection node for `/dev/ttyUSB0`.
  - `_ensure_motor_running` now: `scan_lidar_device` → `start_collision_detection` → defensive `lidar_set_pwm 660` + `lidar_scan` to handle race with the device's async open.

- `dimos-ohmni/src/dimos_ohmni/blueprints/smart.py`
  - Added `wavefront_frontier_explorer()` to the autoconnect chain. Connection → voxel mapper → cost mapper → A* planner ↔ frontier explorer. Verified all transports wire by name.

- `dimos-ohmni/src/dimos_ohmni/blueprints/agentic.py`
  - Rewritten to mirror `unitree_go2_agentic`: `agent()` + `navigation_skill()` + `person_follow_skill(camera_info=OhmniConnection.camera_info_static)` + `speak_skill()` + `web_input()`. Falls back to VLM-only, then to smart, if LLM extras aren't installed.

- `control-app/bot_shell_lidar_dimos.js` (new) — pushed to `/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/bot_shell_lidar.js` on robot, original backed up to `bot_shell_lidar.js.orig`. Routes existing commands through the working device path (`_lidarNode.collisionDetectionNode._lidarDevice`) and adds `lidar_get_scan` / `lidar_get_obstacles` for off-board polling.

- `run_autonomous_scan.py` — entry point that builds the smart blueprint, waits 30s for warmup, and calls `WavefrontFrontierExplorer.explore()` via RPC.

## Adb workaround

Local adb-server on port 5037 caches a "no route to host" against the robot indefinitely after a stale forwarding-process leak. Use a fresh server on 6037 instead:

```bash
ANDROID_ADB_SERVER_PORT=6037 adb start-server
ANDROID_ADB_SERVER_PORT=6037 adb connect 192.168.1.194:5555
```

`run_autonomous_scan.py` sets `ANDROID_ADB_SERVER_PORT=6037` automatically; subprocess `adb` calls inherit it.

## Outstanding

- Physical lidar motor stalled. One verified end-to-end run produced 49 real readings, then subsequent runs return `SCAN_EMPTY|OBS:0,0,0`. Probably stuck rotor; see top-level instructions.
- Once lidar is spinning, `run_autonomous_scan.py` should produce: `lidar scan #1 published: ~50 points` → first `Published frontier goal` → robot moves.

## Acceptance

- Frontier explorer publishes at least one goal pose to `goal_request`.
- Robot drives toward that goal under planner control (`manual_move` commands visible on bot_shell).
- Voxel map persists across goals so subsequent goals are inside *previously unknown* space.
