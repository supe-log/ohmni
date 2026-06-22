# Developer notes — how this was built, and where the bodies are buried

Hard-won knowledge for anyone picking up this robot. The [connection
guide](CONNECTING_TO_A_NEW_ROBOT.md) tells you *how to get on the robot*; this
doc tells you *how the thing actually works, how we programmed it, and what
still doesn't*. Read it before you change anything non-trivial.

> Everything here is current as of the code in this repo. Where the original
> design plan (`.planning/`) and the shipped code disagree, the code wins and
> it's called out.

---

## 1. The mental model: there is no SDK

OhmniLabs ships no robot SDK. We never used their cloud/WebRTC stack to drive the
robot. Instead the entire control path is:

```
your code ──ADB──► bot_shell.sock (a line-oriented command shell on the robot)
```

`bot_shell` is a Node process inside the `com.ohmnilabs.telebot_rtc` app that
exposes a Unix socket. You send it newline-terminated text commands
(`battery`, `manual_move 100 0`, `neck_angle 512`) and it talks to the robot's
motor controllers, servos, LEDs, and sensors. **That socket is the whole API.**
Cameras are the one exception — they're captured out-of-band via V4L2 over
`adb exec-out` (see §6).

Two implementations of this exact channel exist in the repo, and they agree
line-for-line:

- **`control-app/server.js`** — the original Node control server. This is the
  reference: it's the most complete and the most battle-tested. When in doubt
  about the protocol, read this.
- **`dimos-ohmni/src/dimos_ohmni/bridge.py`** — a faithful Python port
  (`OhmniBridge`) used by the dimos blueprints. Its docstrings cite the
  `server.js` line ranges they were ported from.

If you're building something new, you can talk to `bot_shell` from *any*
language — it's just a TCP socket (after the ADB forward) speaking text.

### The `bot_shell` protocol, concretely

- Connect → it emits a banner containing `bot_shell`. Both implementations treat
  "saw the banner" as "ready," and the Python bridge immediately fires
  `wake_head` so the neck servo is live.
- Send `command arg1 arg2\n`. Responses come back as text, sometimes multi-line,
  so the reader accumulates for a short window rather than reading one line.
- It's **stateful and single-session-ish**: the head must be woken before
  `neck_angle` works; the lidar must be initialized in sequence before scans
  return data. There's no request/response ID — you match replies by timing.
- Encoders are read with `apos 0` (left) / `apos 1` (right), returning raw servo
  counts. **The right wheel's deltas are mirrored — negate before integrating.**

---

## 2. How we boot into it and reload firmware

This is the part that wastes everyone's first afternoon, so:

- **ADB is over WiFi on port 5555.** The robot runs `adbd` already; no USB. See
  the connection guide for `adb connect` / `adb root` / `adb forward`.
- **Always use a private ADB server port.** `export ANDROID_ADB_SERVER_PORT=6037`.
  The default server (5037) caches a "no route to host" error indefinitely after
  a stale fork-server leak, and long-running scripts here leak those children.
  The `run_*.py` scripts set this for you; do it by hand for ad-hoc `adb`.
- **`bot_shell` runs as a *forked* `node` process, separate from the Android
  app.** This is the single most important boot fact. Consequences:
  - `am force-stop com.ohmnilabs.telebot_rtc` does **not** reload `bot_shell` —
    the forked `node` keeps the socket open.
  - To reload an edited on-device JS module (e.g. the lidar patch) you must
    `pkill node`, then relaunch the app's face activity so it re-forks:
    ```bash
    adb -s <IP>:5555 shell 'pkill node'
    adb -s <IP>:5555 shell 'am start -n com.ohmnilabs.telebot_rtc/.face.FaceActivity'
    ```
  - After any `node` restart the ADB forward survives, but you must reconnect
    your TCP socket to `:9999`. The bridge does this automatically (recv thread
    detects the close and reconnects after ~3 s).
- **ADB connections drop constantly.** Both `server.js` and `bridge.py` run a
  monitor loop (~10 s) that re-checks `adb devices`, and on reconnect re-roots,
  re-forwards, and re-detects cameras. Build any new long-running tool the same
  way — assume the link dies and self-heals, don't assume a stable socket.
- **`adb root` is required for cameras** (devices are `cameraserver:system`). If
  a unit is locked (`adbd cannot run as root in production builds`), drive / neck
  / lidar still work but USB camera capture won't.

### On-device patching

The only thing we install on the robot is `control-app/bot_shell_lidar_dimos.js`
(see §5). Patch procedure, backup-first, is in the connection guide §9. The
golden rule: **back up the device first** with `ohmni_archive/archive.sh` — it
pulls the APK, the on-device `node-files/` (including the original `bot_shell`
sources), sensor/USB intel, and md5 checksums. Patch only after you have that.

---

## 3. The dimos layer and the blueprint architecture

On top of the raw bridge sits [dimos](https://github.com/dimensionalOS/dimos), a
dataflow robotics framework. The unit of composition is a **`Module`** with typed
`In[...]`/`Out[...]` streams; a **blueprint** wires modules together; a
**coordinator** builds and runs a blueprint.

Our adapter (`dimos-ohmni`) provides:

- **`OhmniConnection`** — the central module. Wraps `OhmniBridge`, publishes
  camera frames / lidar / pointcloud / battery / odometry, consumes `cmd_vel`,
  and exposes the typed skill surface (§4). This is the seam between dimos and
  the robot.
- **Five blueprints** (`dimos_ohmni/blueprints/`), increasing in capability:

  | Blueprint | Adds |
  |---|---|
  | `ohmni-basic` | connection + websocket visualization |
  | `ohmni-smart` | voxel mapper, cost mapper, A\* replanner, frontier explorer |
  | `ohmni-agentic` | LLM Agent, navigation / person-follow / speak skills, web input |
  | `ohmni-free-roam` | `ohmni-smart` + SafetyGovernor |
  | `ohmni-full` | agentic + BrainResearcher + SafetyGovernor + SemanticPin |

  The blueprints are registered into dimos's own registry via the one patch in
  `patches/dimos-robot-all_blueprints.patch` — that's the only reason
  `pip install -e ./dimos-ohmni` makes dimos aware of the Ohmni.

- **The stream-remap trick that makes motion work.** In `ohmni-full`, *both* the
  A\* planner's `cmd_vel` and the websocket-vis module's `cmd_vel` are remapped to
  `raw_cmd_vel`. The SafetyGovernor is then the sole writer of the real
  `cmd_vel`. This matters: without remapping the vis module, its idle
  zero-twists drown the planner's commands on the shared `cmd_vel` stream and the
  robot simply never moves. If you add a new module that emits `cmd_vel`, remap
  it to `raw_cmd_vel` too, or you'll fight the governor.

---

## 4. The skill surface — how the LLM drives the robot

The LLM never sends raw `bot_shell`. It calls **typed `@skill` methods** with
LLM-readable docstrings (units, sign conventions, safe ranges). There are ~28 of
them across the modules:

- **Motion / body** (`connection.py`): `drive`, `stop`, `set_neck_angle`,
  `wake_head`, `rest_head`, `set_led`, `dock`, `is_docking`
- **Sensing** (`connection.py`): `observe` (screen cam), `floor_observe` (floor
  cam), `get_battery`, `get_obstacles`, `get_lidar_scan`
- **Safety** (`safety.py`): `clear_emergency`, `set_geofence`, `set_speed_cap`
- **Memory / world** (`brain.py`, `perception/semantic_pin.py`): `remember`,
  `recall`, `query_label`, `list_pinned_labels`, `coverage_summary`
- **Research** (`web_research.py`): `web_search`, `wiki_search`, `arxiv_search`,
  `read_url`
- **Introspection** (`autoresearch/orchestrator.py`): `autoresearch_status`,
  `autoresearch_recent`, `autoresearch_run_now`

Design rule (`.planning/README.md`, and it held): **skills are typed; no
free-form `bot_shell` from the LLM.** If you want the agent to do something new,
add a skill with a good docstring — don't widen the surface to raw commands.

`drive` units are SI (m/s, rad/s) and get converted to `bot_shell`'s
`manual_move` (mm/s, deg/s) in `drive.py`. Safe range baked into the docstring:
`linear_x ∈ [-0.30, 0.30]`, `angular_z ∈ [-1.0, 1.0]`.

---

## 5. Systems we built

### SafetyGovernor (`safety.py`) — the thing that lets it free-roam at all

Sole consumer of `raw_cmd_vel`, sole producer of `cmd_vel`. Every motion command
passes through it. Defaults:

- caps `max_linear_mps = 0.30`, `max_angular_rps = 1.0`
- `imminent_collision_m = 0.30` — zeroes motion and **latches an emergency** when
  the forward cone is closer than this; the latch auto-clears after the cone has
  been clear for a configurable window (default a few seconds), or on
  `clear_emergency`
- battery lockout: stops at `battery_low_pct = 15`, resumes at
  `battery_resume_pct = 30` (hysteresis so it doesn't chatter)
- a background **watchdog thread** zeroes motion on `raw_cmd_vel` staleness — but
  **once**, not continuously, or it would drown the planner
- stuck-detection and geofence (8 m from home) also zero motion

Non-negotiable per the design and it stuck: free-roam without the governor is off
the table. It's the only Twist consumer; everything publishes through it.

### Odometry (`connection.py`) — wheel encoders, no IMU fusion

Polls `apos 0` / `apos 1` every ~500 ms and integrates differential-drive pose.
Calibration constants, env-overridable:

- `OHMNI_APOS_PER_M = 10860` (≈ 4096 counts/rev ÷ 0.377 m/rev)
- `OHMNI_WHEELBASE_M = 0.30`

Right-wheel deltas are mirrored — negated before integration. Falls back to
dead-reckoning (integrating `cmd_vel`) if an `apos` parse fails. This is what
lets the planner reach goals; verified reaching 3 goals in ~2 min after wiring.

### BrainResearcher (`brain.py`) — local long-term memory + autonomy driver

Appends observations/decisions to `~/.ohmni/brain.md`. Battery-gated, proposes
exploration goals on an interval, docks itself on low battery. `remember` /
`recall` skills read and write it. **Brain is local** — nothing leaves the host
without explicit opt-in.

### SemanticPin (`perception/semantic_pin.py`) — labels at poses

Writes `label @ pose` entries to `~/.ohmni/world.json` (`remember` / `query_label`
/ `list_pinned_labels`). Has a lazy **VLM hook** (`vlm_model="moondream"|"qwen"`)
so a vision model can name what the camera sees; the model only loads on first
call.

### Autoresearch microloops (`autoresearch/`) — self-tuning

A Karpathy-style propose/apply/run/score/rollback loop system with a
weighted-random scheduler (EMA on score deltas) and an append-only TSV journal at
`~/.ohmni/research/journal.tsv`. Five loops ship:

- `CalibrationLoop` — tunes the odometry + safety constants
- `ExplorationTuningLoop` — tunes frontier-exploration knobs (hot-reloaded)
- `SkillProbeLoop` — single-skill smoke experiments (say, led, drive, …)
- `WebResearchLoop` / `GitHubResearchLoop` — inject external findings into the brain

**Iron rule, and respect it:** every knob mutation is *configuration* (env files,
JSON), never source. The orchestrator never edits `.py`. Knob ranges are clamped
per-loop; failed ticks roll back. Pause everything with `OHMNI_AUTORESEARCH=0`.

### Face (`face.py`) and audio (`audio.py`)

- **Face**: serves an SVG face over HTTP from the host and launches Chrome in
  kiosk mode on the robot's Android tablet pointed at it.
- **Audio**: TTS via `bot_shell say`; mic capture via `adb exec-out tinycap`
  *if it exists on the device* (checked lazily), with graceful fallback. Don't
  assume the mic works on an arbitrary unit.

### LiDAR (`lidar.py` + `control-app/bot_shell_lidar_dimos.js`)

The RPLidar A2M8 is the messiest subsystem. Stock firmware **cannot initialize
the lidar device from boot** — `cmd_*` lidar commands return "There is no lidar
device" because `_botnode._lidarDevice` is always null on this firmware. The
patch routes commands through the working path
(`_botnode._lidarNode.collisionDetectionNode._lidarDevice`) and adds off-board
polling verbs. Init sequence: `scan_lidar_device` → `start_collision_detection`
→ `lidar_get_scan` (returns `SCAN:angle:dist,...|OBS:...`).

The patch exposes: `scan_lidar_device`, `update_lidar_config`, `lidar_set_pwm`,
`lidar_scan`, `lidar_stop`, `lidar_release`, `lidar_get_obstacles`,
`lidar_get_scan`, `lidar_status`, **`lidar_get_scan_v2`**, `lidar_tap`,
`lidar_restart`. `lidar_get_scan_v2` is an *independent* express-scan decoder
that taps the SerialPort and parses the 84-byte packets itself — it keeps working
when the stock parser wedges in "waiting for descriptor" mode (which it does
after the first express response, making `lidar_get_scan` return `SCAN_EMPTY`
forever). The Python side (`lidar.py`) has its own decoder too.

Geometry: front-mounted, 0°=front, 90°=left, 180°=back, 270°=right. The robot
body reads ~177–190 mm from many angles — there's a body-signature distance
filter to drop those returns.

---

## 6. Cameras (the out-of-band path)

Cameras do **not** go through `bot_shell`. The robot's FFmpeg is compiled
`--disable-avdevice --disable-devices`, so it can't capture — we use
`v4l2-ctl --stream-mmap` and stream MJPEG out over `adb exec-out`. Two cameras:
`See3CAM_CU135` (screen, up to 4K) and `HD USB Camera` (floor, up to 1080p).

**Detect cameras by V4L2 card name, never by `/dev/videoN`** — the device numbers
reshuffle after every USB re-enumeration. `camera.py` and `server.js` both match
on card name (`See3CAM` → screen, `HD USB Camera` → floor). Throughput is ~1–2
FPS, which is fine for VLM queries and the command center but is not a video
stream.

---

## 7. Gaps, sharp edges, and things that don't work

Be honest with yourself about these before you promise anyone a demo.

**Hardware / firmware**
- **LiDAR motor stall (unsolved, hardware).** After a few start/stop cycles the
  A2M8 rotor can't break static friction and `lidar_get_scan` returns
  `SCAN_EMPTY|OBS:0,0,0` indefinitely — battery is fine, PWM commands don't spin
  it back up. Recovery is physical, in order: finger-nudge the dome →
  power-cycle the robot → unplug/replug the cp210x USB cable. Treat persistent
  `SCAN_EMPTY` as a hardware state, not a code bug.
- **Stock lidar is dead without the patch.** A fresh robot returns "There is no
  lidar device" until you install `bot_shell_lidar_dimos.js` (§5, and connection
  guide §9).
- **Root may be locked** on some units → no USB camera capture (§2).
- **No guaranteed mic.** `tinycap` may be absent; audio-in is best-effort.

**Connection / ops**
- **ADB is flaky by nature** — drops, goes `offline`, caches stale errors. The
  6037 port workaround and the reconnect monitor are mandatory scaffolding, not
  nice-to-haves.
- **The robot IP is hardcoded** in five files (`run_ohmni*.py`, `types.py`,
  `server.js`, `archive.sh`). There is no service discovery — set a DHCP
  reservation and update the five files (connection guide §7).
- **Reloading on-device JS is a `pkill node` dance**, not a clean restart (§2).

**Software architecture**
- **The component-swap provider abstraction was never built.** `.planning/`
  describes `LidarProvider` / `DriveProvider` Protocols, a `SyntheticLidarProvider`
  for robot-free dev, and a `RecordedLidarProvider` for regression replay. None
  of these exist in the code — there are no `*Provider` classes. Swapping a
  sensor implementation today still means editing the blueprint and the module.
  If you want offline/CI development without a physical robot, **this is the
  highest-value thing to build**, and the design is already written down for you.
- **Odometry is encoder-only**, no IMU fusion — heading drifts over long runs.
  The IMU was captured in the Stage-0 archive (`device_info/sensors.txt`) but is
  not wired into pose estimation.
- **`dimos/` is pinned to one commit (`a035fb3`)** and carries a hand-applied
  patch. Upstream churn will eventually break the patch; it's 8 lines, reapply by
  hand (see `HOST_SETUP.md`). We are not tracking upstream.
- **The agent flow needs a paid LLM** (`OPENAI_API_KEY`) for the chat at :8765 and
  multi-step skill chains. TTS, VLM, web search, and repo mining all run on free
  local models — see `dimos-ohmni/src/dimos_ohmni/autoresearch/README.md` for the
  cost breakdown and local-model setup.

---

## 8. If you're starting fresh, do it in this order

1. [Connect](CONNECTING_TO_A_NEW_ROBOT.md) — same network, ADB, forward, smoke-test.
2. **Archive the device** (`ohmni_archive/archive.sh`) before touching firmware.
3. Update the IP in the five files.
4. [Set up the host stack](HOST_SETUP.md) — clone+patch dimos, venv, install.
5. Apply the lidar patch (only if you need scans).
6. `python run_ohmni.py`, watch <http://localhost:8765>, confirm it maps and moves.
7. Then, and only then, turn on the agent / autoresearch.
</content>
