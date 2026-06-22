# ohmni

Autonomy stack for the **OhmniLabs Ohmni 5-2** telepresence robot. It drives the
robot from a host machine over WiFi — no SDK, no cloud — by speaking to the
robot's on-device command shell (`bot_shell`) through ADB, and layers the
[dimos](https://github.com/dimensionalOS/dimos) robotics framework on top for
SLAM, navigation, frontier exploration, an LLM agent, and a safety governor.

> **New here / setting up a robot?** Start with
> **[docs/CONNECTING_TO_A_NEW_ROBOT.md](docs/CONNECTING_TO_A_NEW_ROBOT.md)** — the
> exact, step-by-step method for getting a fresh Ohmni on the same network and
> talking to it. Then [docs/HOST_SETUP.md](docs/HOST_SETUP.md) for the Python stack.

---

## How it works

```
host (Mac/Linux)                         Ohmni 5-2 (Android)
┌─────────────────────────┐   WiFi/LAN   ┌──────────────────────────────┐
│ dimos + dimos-ohmni      │  ADB :5555   │ com.ohmnilabs.telebot_rtc    │
│   blueprints / skills    │ ───────────► │   adbd                       │
│ OhmniBridge (bridge.py)  │  fwd :9999   │   bot_shell.sock ──► firmware│
│   ▲ TCP 127.0.0.1:9999   │ ───────────► │   (drive, neck, LED, battery,│
│ control-app/server.js    │              │    cameras, RPLidar A2M8)    │
└─────────────────────────┘              └──────────────────────────────┘
```

Nothing is installed on the robot for the core connection — it's plain ADB plus a
port-forward to the `bot_shell` Unix socket. The only on-device change is an
optional lidar patch (`control-app/bot_shell_lidar_dimos.js`).

## Repository layout

| Path | What it is |
|---|---|
| `docs/` | **Setup & connection documentation** (start here) |
| `dimos-ohmni/` | The adapter package: dimos `Module`, blueprints, and the camera/lidar/drive/audio/telemetry bridges. The core of this project. |
| `control-app/` | Original Node control server (`server.js`) — the reference implementation of the connection method — plus the lidar patch and a web control panel. |
| `run_ohmni.py` | Launch the `ohmni-smart` blueprint (SLAM + nav + exploration). |
| `run_ohmni_full.py` | Launch `ohmni-full` (agent + brain + safety + semantic pin). |
| `run_autonomous_scan.py` | Phase 0 entry point — boots `ohmni-smart`, triggers frontier exploration. |
| `patches/` | `dimos-robot-all_blueprints.patch` — the one local change to upstream dimos. |
| `ohmni_archive/archive.sh` | Device backup script (pull APK, firmware, device intel). |
| `PROTOCOL_ANALYSIS.md` | Reverse-engineering notes on the OhmniLabs WebRTC web-app protocol. |
| `.planning/` | The 5-phase autonomy roadmap and per-phase notes. |
| `ohmni_*.js` | Reverse-engineered TeleBot web-app bundles (reference for the protocol work). |

### Blueprint variants (in `dimos-ohmni/src/dimos_ohmni/blueprints/`)

- `ohmni-basic` — connection + visualization
- `ohmni-smart` — basic + voxel mapper + cost mapper + A\* planner + frontier explorer
- `ohmni-agentic` — smart + LLM Agent + navigation/person-follow/speak skills + web input
- `ohmni-free-roam` — smart + SafetyGovernor
- `ohmni-full` — agentic + BrainResearcher + SafetyGovernor + SemanticPin

The **SafetyGovernor** is the sole writer of `cmd_vel`: it caps speed
(0.30 m/s, 1.0 rad/s) and zeroes motion on imminent collision, low battery,
stuck-detection, and geofence. Free-roam without it is off the table.

## Quickstart

```bash
# 1. Connect to the robot — see docs/CONNECTING_TO_A_NEW_ROBOT.md
export ANDROID_ADB_SERVER_PORT=6037
adb connect <ROBOT_IP>:5555 && adb -s <ROBOT_IP>:5555 root
adb -s <ROBOT_IP>:5555 forward tcp:9999 \
    localfilesystem:/data/data/com.ohmnilabs.telebot_rtc/files/bot_shell.sock

# 2. Host stack — see docs/HOST_SETUP.md
git clone https://github.com/dimensionalOS/dimos.git
cd dimos && git checkout a035fb315d35bba511dfc6156dc21827e70dbc94 \
  && git apply ../patches/dimos-robot-all_blueprints.patch && cd ..
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ./dimos -e ./dimos-ohmni langchain-core

# 3. Point the code at your robot's IP (replaces 192.168.1.194) and run
python run_ohmni.py            # web UI on http://localhost:8765
```

## Not included in this repo (and why)

To keep the repo a reasonable size, three large, regenerable things are
gitignored:

- **`dimos/`** — upstream framework (~31 GB with runtime data). Re-clone at the
  pinned commit and apply `patches/`; see [docs/HOST_SETUP.md](docs/HOST_SETUP.md).
- **`ohmni_archive/`** (except `archive.sh`) — ~1.5 GB device backup (APK, app-data
  tar, firmware). Recreate with `bash ohmni_archive/archive.sh` against a live robot.
- **`.venv/`** — the Python virtualenv. Recreate per the setup docs.

## Hardware notes

- Ohmni 5-2: differential drive, neck servo, LED ring, speaker, Android tablet face.
- Cameras: `See3CAM_CU135` (screen, 13 MP) + `HD USB Camera` (floor). Detect by
  **card name**, not `/dev/videoN`.
- LiDAR: RPLidar A2M8, front-mounted, via a cp210x USB-UART at `/dev/ttyUSB0`.
- Compute split: the host runs dimos + agents; the robot runs the `telebot_rtc`
  Node bridge.

## License / provenance

`dimos` is Apache 2.0 (upstream). `PROTOCOL_ANALYSIS.md` and the `ohmni_*.js`
bundles are reverse-engineering notes/artifacts of the OhmniLabs web app for
interoperability with hardware you own. No OhmniLabs credentials or API keys are
included in this repository.
</content>
