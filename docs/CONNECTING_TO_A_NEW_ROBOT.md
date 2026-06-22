# Connecting to a fresh Ohmni robot

This is the exact method this project uses to talk to an **OhmniLabs Ohmni 5-2**
telepresence robot from a host Mac/Linux machine — the same approach baked into
`control-app/server.js` and `dimos-ohmni`'s `bridge.py`. Follow it top to bottom
the first time you set up a new robot.

> **The model in one sentence:** the Ohmni is an Android device running the
> `com.ohmnilabs.telebot_rtc` app. You reach it over the LAN with **ADB over
> WiFi**, then talk to the robot's firmware through a Unix socket called
> **`bot_shell`** that you expose to your host with an ADB port-forward. Nothing
> is installed on the robot to make this work (the lidar patch in
> [step 8](#8-optional-apply-the-lidar-patch) is the one exception, and it's optional).

```
host (Mac)  ──WiFi/LAN──►  Ohmni (Android)
   adb connect <ip>:5555          adbd  (tcp 5555)
   adb forward tcp:9999  ─────►   bot_shell.sock  ─►  robot firmware
   TCP 127.0.0.1:9999             (drive, neck, LEDs, battery, lidar, …)
```

---

## 0. Prerequisites (one-time, on the host)

- **`adb`** (Android Platform Tools). macOS: `brew install android-platform-tools`.
- **`v4l2-ctl`** is only needed *on the robot* (already present); you do not need it on the host.
- The robot and your host must be on the **same network / subnet** (see step 1).
- For the full Python stack later, see [`HOST_SETUP.md`](HOST_SETUP.md).

Verify adb:

```bash
adb version
```

---

## 1. Put the robot and host on the same network

1. Power on the Ohmni and let it finish booting to the face screen.
2. On the robot's touchscreen, join your WiFi network (Settings → WiFi). Use the
   **same** SSID/subnet your host is on. A wired host on the same router works too.
3. Confirm the host can reach that subnet (e.g. you're on `192.168.1.x`).

> Enterprise/guest WiFi that isolates clients ("AP isolation" / "client
> isolation") will block ADB. If `adb connect` later times out but the robot is
> clearly online, this is the usual culprit — use a network where devices can
> see each other.

---

## 2. Find the new robot's IP address

Pick whichever is easiest:

- **On the robot:** Settings → WiFi → tap the connected network → it shows the IP.
- **From your router's** DHCP/client list (look for an `OhmniLabs` / Android host).
- **Scan the subnet** for the open ADB port (5555):

  ```bash
  # macOS/Linux — replace 192.168.1 with your subnet
  nmap -p 5555 --open 192.168.1.0/24
  ```

  The host that answers on 5555 is almost certainly the robot.

Write the IP down — call it `ROBOT_IP`. The robot this project was built against
was `192.168.1.194`; a fresh robot will differ, so you'll update that value in
[step 7](#7-point-the-code-at-the-new-ip).

> **Tip:** give the robot a DHCP reservation (static lease) in your router so its
> IP doesn't change on reboot. Everything below assumes a stable IP.

---

## 3. Connect over ADB (WiFi)

The Ohmni already runs `adbd` listening on TCP **5555**, so no USB cable is needed.

```bash
adb connect <ROBOT_IP>:5555
adb devices          # should list <ROBOT_IP>:5555  device
```

**Use a private ADB server port.** The default adb server (port 5037) caches a
"no route to host" error indefinitely after a stale fork-server leak, which is a
recurring problem with the long-running scripts here. Always export this first
(the `run_*.py` scripts set it automatically):

```bash
export ANDROID_ADB_SERVER_PORT=6037
adb start-server
adb connect <ROBOT_IP>:5555
```

If `adb devices` shows `offline`, recover with:

```bash
adb kill-server && adb start-server && adb connect <ROBOT_IP>:5555
```

---

## 4. Get root (needed for cameras and some device files)

The camera devices are owned by `cameraserver:system`, so capture needs root.

```bash
adb -s <ROBOT_IP>:5555 root
# adbd restarts as root — reconnect:
adb connect <ROBOT_IP>:5555
```

Ohmni firmware ships `adb root`-capable (`ro.debuggable=1`). If `adb root`
returns "adbd cannot run as root in production builds," this particular unit is
locked down — drive/neck/lidar over `bot_shell` still work, but USB camera
capture will not.

---

## 5. Forward the `bot_shell` socket to your host

`bot_shell` is a Unix domain socket the robot app exposes. Forward it to a local
TCP port (this project uses **9999**):

```bash
adb -s <ROBOT_IP>:5555 forward tcp:9999 \
    localfilesystem:/data/data/com.ohmnilabs.telebot_rtc/files/bot_shell.sock
```

Now `127.0.0.1:9999` on your host is a direct line to the robot's command shell.

---

## 6. Talk to the robot (smoke test)

Open the forwarded socket and send a command. On connect, `bot_shell` prints a
banner like `Connected to ohmni bot_shell.` — that's how the bridge knows it's ready.

```bash
# nc keeps the socket open; type commands, Ctrl-C to exit
nc 127.0.0.1 9999
# then type:  battery        -> returns charge level
#             wake_head      -> activates the neck servo
#             neck_angle 512 -> center the head (400=down, 512=center, 600=up)
#             say hello       -> text-to-speech
#             manual_move 0 0 -> stop / no motion
```

> **Important — wake the head first.** `neck_angle` is ignored until you've sent
> `wake_head`. The Python bridge does this automatically the moment it sees the
> banner. Send `rest_head` / `sleep_head` to release the servo.

If you get the banner and `battery` responds, **the connection is fully working.**
Everything else (cameras, lidar, drive) rides on this same channel.

### Command reference (most-used `bot_shell` verbs)

| Command | What it does | Notes |
|---|---|---|
| `battery` | Battery telemetry | level %, charging state |
| `wake_head` / `rest_head` | Enable / release neck servo | call `wake_head` before `neck_angle` |
| `neck_angle <400–600>` | Tilt head | 400 down · 512 center · 600 up (10-bit) |
| `manual_move <mm/s> <deg/s>` | Differential drive | linear + angular; `0 0` stops |
| `apos 0` / `apos 1` | Left / right wheel encoder | raw counts; right wheel deltas are mirrored |
| `say <text>` | Text-to-speech | speaker |
| `set_led ...` | LED ring | |

Cameras are captured separately with V4L2 over `adb exec-out` (not via
`bot_shell`) — see `dimos-ohmni`'s `camera.py`. Detect them by **card name**, not
`/dev/videoN` (device numbers shuffle after USB re-enumeration):
`See3CAM_CU135` = screen camera, `HD USB Camera` = floor camera.

---

## 7. Point the code at the new IP

The robot IP `192.168.1.194` is hardcoded in a few places. Update all of them to
your `ROBOT_IP`:

| File | What to change |
|---|---|
| `run_ohmni.py` | `global_config.update(robot_ip="...")` |
| `run_ohmni_full.py` | `global_config.update(robot_ip="...")` |
| `dimos-ohmni/src/dimos_ohmni/types.py` | `OhmniConfig.ip` default |
| `control-app/server.js` | `const ROBOT_IP = '...'` |
| `ohmni_archive/archive.sh` | `ROBOT="...:5555"` |

Quick find/replace from the repo root (review the diff before committing):

```bash
grep -rl "192.168.1.194" --include="*.py" --include="*.js" --include="*.sh" .
# then replace, e.g. with sed (macOS):
grep -rl "192.168.1.194" --include="*.py" --include="*.js" --include="*.sh" . \
  | xargs sed -i '' 's/192\.168\.1\.194/<ROBOT_IP>/g'
```

---

## 8. (Recommended) Back up the robot before changing anything

Before you patch firmware, archive the device so you can recover and so you have
a reference image. This pulls the APK, the on-device `bot_shell` Node sources,
sensor/USB intel, and checksums:

```bash
export ANDROID_ADB_SERVER_PORT=6037
bash ohmni_archive/archive.sh      # writes into ohmni_archive/ (gitignored)
```

The output (APK, app-data tar, firmware, device_info/) is intentionally **not**
committed to git — it's ~1.5 GB of device backup. Keep it somewhere safe.

---

## 9. (Optional) Apply the lidar patch

Only needed if you want lidar scans through the stock firmware, which **cannot
initialize the lidar device from boot** (it returns `There is no lidar device`).
The patch in `control-app/bot_shell_lidar_dimos.js` routes lidar commands through
the working device path and adds off-board polling verbs
(`lidar_get_scan` / `lidar_get_scan_v2` / `lidar_get_obstacles`).

```bash
# 1. Back up the stock module
adb -s <ROBOT_IP>:5555 shell 'cp \
  /data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/bot_shell_lidar.js \
  /data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/bot_shell_lidar.js.orig'

# 2. Push the patched module
adb -s <ROBOT_IP>:5555 push control-app/bot_shell_lidar_dimos.js \
  /data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/bot_shell_lidar.js

# 3. Reload: bot_shell runs in a forked `node` process — force-stop alone does
#    NOT reload it. Kill node, then relaunch the app's face activity.
adb -s <ROBOT_IP>:5555 shell 'pkill node'
adb -s <ROBOT_IP>:5555 shell 'am start -n com.ohmnilabs.telebot_rtc/.face.FaceActivity'
```

Then re-forward the socket (step 5) and bring lidar up:

```
scan_lidar_device → start_collision_detection → lidar_get_scan
# returns:  SCAN:angle:dist,...|OBS:...
```

**Known hardware quirk:** after a few start/stop cycles the RPLidar A2M8 rotor
sometimes can't break static friction and `lidar_get_scan` returns
`SCAN_EMPTY|OBS:0,0,0` forever. This is *not* a software bug. Recover, in order:
(1) finger-nudge the lidar dome, (2) power-cycle the robot, (3) unplug/replug the
cp210x USB cable. `lidar_get_scan_v2` (independent decoder) keeps working when
the stock parser wedges.

---

## 10. Run the stack

Once the connection works and the IP is updated:

```bash
source .venv/bin/activate          # see HOST_SETUP.md to create it
export ANDROID_ADB_SERVER_PORT=6037

python run_ohmni.py                # ohmni-smart: SLAM + nav + exploration
# or, full autonomy (needs OPENAI_API_KEY):
export OPENAI_API_KEY=sk-...
python run_ohmni_full.py           # agent + brain + safety + semantic-pin
```

Web UI: <http://localhost:8765>. Stop with **Ctrl-C** (the SafetyGovernor
guarantees a final zero-twist before tear-down).

---

## Troubleshooting quick reference

| Symptom | Fix |
|---|---|
| `adb connect` times out, robot is online | AP/client isolation on the WiFi; use a normal LAN. Confirm port 5555 with `nmap -p 5555`. |
| `adb devices` shows `offline` | `adb kill-server && adb start-server && adb connect <ROBOT_IP>:5555` |
| "no route to host" that never clears | You're on the default adb server. `export ANDROID_ADB_SERVER_PORT=6037` and restart. |
| `bot_shell` connects but `neck_angle` does nothing | Send `wake_head` first. |
| Cameras not found | `adb root` (capture needs root); match by **card name**, not `/dev/videoN`. |
| `There is no lidar device` | Apply the lidar patch (step 9). |
| `SCAN_EMPTY` forever | Hardware stall — nudge the dome / power-cycle / replug cp210x (step 9). |
| IP changed after reboot | Set a DHCP reservation for the robot; re-run step 7. |

---

## What lives where

- **`control-app/server.js`** — the original Node control server; the canonical,
  battle-tested implementation of this connection method (ADB persistence,
  forward, bot_shell socket, camera detection).
- **`dimos-ohmni/src/dimos_ohmni/bridge.py`** — Python port of the same logic,
  used by the dimos blueprints.
- **`ohmni_archive/archive.sh`** — device backup script (step 8).
- **`control-app/bot_shell_lidar_dimos.js`** — the on-device lidar patch (step 9).

---

## Going deeper

Once you're connected, **[DEVELOPER_NOTES.md](DEVELOPER_NOTES.md)** explains how
the robot is actually programmed (the `bot_shell` protocol, the typed skill
surface), how booting and on-device patching really work (the forked `node`
process), every system built on top (SafetyGovernor, odometry, brain,
autoresearch, lidar decoders), and the known gaps and failure modes.
</content>
