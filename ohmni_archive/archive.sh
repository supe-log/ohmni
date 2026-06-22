#!/usr/bin/env bash
# Stage 0: Archive Ohmni device software
# Run this when the robot is powered on and reachable at 192.168.1.194
# Requires: adb installed, robot on the same network
set -euo pipefail

ROBOT="192.168.1.194:5555"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Ohmni Software Archive ==="
echo "Target: $ROBOT"
echo "Output: $DIR"
echo ""

# --- Connect and root ---
echo "[1/7] Connecting to ADB..."
adb connect "$ROBOT" || true
sleep 1
adb -s "$ROBOT" root
sleep 2
adb connect "$ROBOT"
sleep 1

echo "[2/7] Collecting device intelligence..."
mkdir -p "$DIR/device_info"

adb -s "$ROBOT" shell getprop > "$DIR/device_info/build.prop.txt" 2>&1 || true
adb -s "$ROBOT" shell cat /proc/version > "$DIR/device_info/kernel_version.txt" 2>&1 || true
adb -s "$ROBOT" shell uname -a > "$DIR/device_info/uname.txt" 2>&1 || true
adb -s "$ROBOT" shell cat /sys/kernel/debug/usb/devices > "$DIR/device_info/usb_devices.txt" 2>&1 || true
adb -s "$ROBOT" shell 'for d in /sys/bus/usb/devices/*/product; do echo "$d: $(cat $d 2>/dev/null)"; done' > "$DIR/device_info/usb_products.txt" 2>&1 || true
adb -s "$ROBOT" shell 'for d in /dev/video*; do echo "=== $d ==="; v4l2-ctl --device=$d --all 2>/dev/null; done' > "$DIR/device_info/v4l2_all.txt" 2>&1 || true
adb -s "$ROBOT" shell ps -A > "$DIR/device_info/process_tree.txt" 2>&1 || true
adb -s "$ROBOT" shell dumpsys activity services > "$DIR/device_info/android_services.txt" 2>&1 || true
adb -s "$ROBOT" shell pm list packages -f > "$DIR/device_info/installed_packages.txt" 2>&1 || true
adb -s "$ROBOT" shell ip addr > "$DIR/device_info/ip_addr.txt" 2>&1 || true
adb -s "$ROBOT" shell netstat -tlnp > "$DIR/device_info/netstat.txt" 2>&1 || true

# IMU / sensor check
echo "[2b] Checking sensors (IMU)..."
adb -s "$ROBOT" shell dumpsys sensorservice > "$DIR/device_info/sensors.txt" 2>&1 || true
adb -s "$ROBOT" shell 'for e in /dev/input/event*; do echo "== $e =="; getevent -p $e 2>&1; done' > "$DIR/device_info/input_events.txt" 2>&1 || true

# Audio check
echo "[2c] Checking audio devices..."
adb -s "$ROBOT" shell 'which tinycap 2>/dev/null; which tinyplay 2>/dev/null; ls -la /dev/snd/ 2>/dev/null' > "$DIR/device_info/audio_devices.txt" 2>&1 || true

# Chrome/WebView check
echo "[2d] Checking browser availability..."
adb -s "$ROBOT" shell "pm list packages | grep -E 'chrome|webview'" > "$DIR/device_info/browsers.txt" 2>&1 || true

echo "[3/7] Pulling APK..."
mkdir -p "$DIR/apk"
APK_PATH=$(adb -s "$ROBOT" shell pm path com.ohmnilabs.telebot_rtc 2>/dev/null | sed 's/package://' | tr -d '\r\n')
if [ -n "$APK_PATH" ]; then
    adb -s "$ROBOT" pull "$APK_PATH" "$DIR/apk/telebot_rtc.apk"
    echo "  APK pulled: $APK_PATH"
else
    echo "  WARNING: Could not find telebot_rtc APK path"
fi

echo "[4/7] Pulling app data (tar)..."
mkdir -p "$DIR/app_data"
adb -s "$ROBOT" shell 'cd /data/data/com.ohmnilabs.telebot_rtc && tar cf - --exclude=cache --exclude=code_cache --exclude=*.log . 2>/dev/null' > "$DIR/app_data/telebot_rtc_data.tar" || true

echo "[4b] Pulling bot_shell + Node runtime..."
mkdir -p "$DIR/firmware"
adb -s "$ROBOT" pull /data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/ "$DIR/firmware/node-files/" 2>/dev/null || true

echo "[5/7] Pulling system files..."
mkdir -p "$DIR/system"
adb -s "$ROBOT" pull /system/build.prop "$DIR/system/build.prop" 2>/dev/null || true
mkdir -p "$DIR/system/init"
adb -s "$ROBOT" shell ls /system/etc/init/ > "$DIR/system/init_scripts.txt" 2>/dev/null || true
adb -s "$ROBOT" pull /system/etc/init/ "$DIR/system/init/" 2>/dev/null || true

echo "[6/7] Creating file checksums..."
adb -s "$ROBOT" shell 'find /data/data/com.ohmnilabs.telebot_rtc/files -type f -exec md5sum {} \; 2>/dev/null' > "$DIR/app_data/file_checksums.md5" || true

echo "[7/7] Recording archive timestamp..."
echo "Archived at: $(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$DIR/MANIFEST.txt"
echo "ADB target: $ROBOT" >> "$DIR/MANIFEST.txt"
echo "Host: $(hostname)" >> "$DIR/MANIFEST.txt"
echo "" >> "$DIR/MANIFEST.txt"
echo "Contents:" >> "$DIR/MANIFEST.txt"
find "$DIR" -type f | sort >> "$DIR/MANIFEST.txt"

echo ""
echo "=== Archive complete ==="
echo "Files stored in: $DIR"
echo ""
echo "Key findings to check:"
echo "  - Sensors/IMU: $DIR/device_info/sensors.txt"
echo "  - Audio (tinycap): $DIR/device_info/audio_devices.txt"
echo "  - Browsers: $DIR/device_info/browsers.txt"
echo "  - Bot shell source: $DIR/firmware/node-files/"
