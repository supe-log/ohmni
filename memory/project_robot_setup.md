---
name: Ohmni Robot Setup
description: ADB connection, bot_shell socket path, USB device mapping, LIDAR setup, camera capture method, neck servo commands
type: project
---

## ADB Connection
- Robot IP: 192.168.1.194:5555 (WiFi ADB)
- ADB forward for bot_shell: `adb forward tcp:9999 localfilesystem:/data/data/com.ohmnilabs.telebot_rtc/files/bot_shell.sock`
- After ADB connect, run `adb root` then set up forward

## USB Device Mapping (on robot)
- **ttyUSB0** (CP2102, Silicon Labs, ID 10c4:ea60, USB port 1-7.1.1.4): **RPLidar** — `/dev/usb/tty1-7.1` symlink
- **ttyUSB1** (FT230X, FTDI, ID 0403:6015, USB port 1-2.1): **Robot MCU** (motors/servos) — `/dev/usb/tty1-2.1` symlink
- Device scanner uses `/dev/usb/` symlinks, not `/dev/ttyUSB*` directly

## LIDAR Setup
- RPLidar A2M8 connected via CP2102 USB-UART at 115200 baud
- Robot firmware files: `lidar_serial.js`, `lidar_node.js`, `collision_detection.js`, `bot_shell_lidar.js`
- Scans for LIDAR by product ID `10c4/ea60` in uevent files
- Bot_shell commands to initialize: `scan_lidar_device` → `start_collision_detection` → `lidar_scan` → `toggle_collision_detection on`
- LIDAR is NOT auto-started on boot — must be explicitly initialized via bot_shell commands
- Default collision config: rotation 660Hz, front warn 1250mm, front stop 750mm, back warn 1350mm, back stop 550mm

**Why:** The LIDAR doesn't auto-start because the node process only opens ttyUSB1 (MCU) at boot. The LIDAR device (ttyUSB0) needs explicit scan_lidar_device command.

**How to apply:** After robot restart, always run the LIDAR init sequence via bot_shell or the control-app /api/lidar/scan-device endpoint.

## Camera Capture
- Floor cam: HD USB Camera at /dev/video1 (640x480 MJPEG via v4l2-ctl)
- Screen cam: See3CAM_CU135 at /dev/video0 (640x480 MJPEG via v4l2-ctl)
- Both captured via `adb exec-out v4l2-ctl --stream-mmap`

## Neck Servo
- Range: 250-550 (center ~512)
- Commands: `neck_angle <value>`, `wake_head`, `rest_head`

## Ohmni App
- Main process: `com.ohmnilabs.telebot_rtc` running Node.js at `/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/app.js`
- Bot ID: b076c7f99a073176d121f5502e9dac6008a232531455fc6bb1cd1c693893d938
