"""OhmniConnection — dimos Module for the Ohmni 5-2 telepresence robot.

Drives the Ohmni from a host PC over ADB + bot_shell, exposing:
- Two cameras (screen + floor) as Image streams
- RPLidar A2M8 as PointCloud2 stream
- Twist velocity input -> bot_shell motor commands
- Head servo control
- Battery/telemetry
- Audio (say) and face rendering

Follows the GO2Connection pattern from dimos/robot/unitree/go2/connection.py.
"""

import math
import threading
import time

import numpy as np
from reactivex.disposable import Disposable

from dimos import spec
from dimos.utils.logging_config import setup_logger
from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs import (
    PoseStamped,
    Quaternion,
    Transform,
    Twist,
    Vector3,
)
from dimos.msgs.sensor_msgs import CameraInfo, Image, PointCloud2

from .bridge import OhmniBridge
from .camera import OhmniCameraStream
from .drive import OhmniDriveTrain
from .lidar import OhmniLidarReader
from .telemetry import OhmniTelemetry
from .types import OhmniConfig

logger = setup_logger()


def _camera_info_static() -> CameraInfo:
    """Static intrinsics for the Ohmni screen camera.

    Approximate values for the See3CAM_CU135 at 320x240 — replace with a
    proper calibration when one is available. Skills like person_follow
    consume this at blueprint construction time, before the connection
    is started.
    """
    width, height = 320, 240
    fx, fy = 250.0, 250.0
    cx, cy = float(width / 2), float(height / 2)
    return CameraInfo(
        frame_id="camera_optical",
        width=width,
        height=height,
        distortion_model="plumb_bob",
        D=[0.0, 0.0, 0.0, 0.0, 0.0],
        K=[fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0],
        R=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        P=[fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0],
    )


class OhmniConnection(Module, spec.Camera, spec.Pointcloud):
    """Dimos module for the Ohmni 5-2 telepresence robot.

    Communicates with the Ohmni over ADB WiFi + bot_shell socket.
    Exposes cameras, LiDAR, drive, head, battery as dimos streams.
    """

    # -- Dimos streams --
    cmd_vel: In[Twist]
    color_image: Out[Image]       # Screen camera (13MP See3CAM)
    floor_image: Out[Image]       # Floor camera (HD USB)
    camera_info: Out[CameraInfo]
    lidar: Out[PointCloud2]
    pointcloud: Out[PointCloud2]  # Alias for spec.Pointcloud
    odom: Out[PoseStamped]
    battery: Out[dict]

    # Static camera intrinsics — consumed by skills (e.g. person_follow)
    # at blueprint construction time, before the connection is started.
    camera_info_static: CameraInfo = _camera_info_static()

    # -- Internal state --
    _bridge: OhmniBridge
    _screen_cam: OhmniCameraStream
    _floor_cam: OhmniCameraStream
    _lidar_reader: OhmniLidarReader
    _drive: OhmniDriveTrain
    _telemetry: OhmniTelemetry
    _latest_video_frame: Image | None = None
    _latest_floor_frame: Image | None = None
    _camera_info_thread: threading.Thread | None = None
    _odom_thread: threading.Thread | None = None

    # Dead-reckoning odometry (until we find a bot_shell odom command)
    _odom_x: float = 0.0
    _odom_y: float = 0.0
    _odom_theta: float = 0.0
    _last_cmd_time: float = 0.0

    # Wheel-encoder odometry (apos 0 = left, apos 1 = right). Calibration
    # constants are estimates for Ohmni 5-2; tune via env if drift is bad.
    # APOS_PER_M = encoder counts per metre of wheel travel.
    # WHEELBASE_M = distance between left/right wheels (metres).
    _wheel_left_last: int | None = None
    _wheel_right_last: int | None = None

    def __init__(
        self,
        ip: str = "192.168.1.194",
        config: OhmniConfig | None = None,
        *args,
        **kwargs,
    ) -> None:
        self._config = config or OhmniConfig(ip=ip)
        self._bridge = OhmniBridge(self._config)
        self._drive = OhmniDriveTrain(self._bridge)
        self._lidar_reader = OhmniLidarReader(
            adb_addr=f"{self._config.ip}:{self._config.adb_port}",
            config=self._config,
            on_scan=self._on_lidar_scan,
            bridge=self._bridge,
        )
        self._screen_cam = OhmniCameraStream(
            self._bridge, cam_id=0, on_frame=self._on_screen_frame
        )
        self._floor_cam = OhmniCameraStream(
            self._bridge, cam_id=1, on_frame=self._on_floor_frame
        )
        self._telemetry = OhmniTelemetry(self._bridge)

        Module.__init__(self, *args, **kwargs)

    @rpc
    def start(self) -> None:
        super().start()

        # Subscribe cmd_vel input to drive
        self._disposables.add(Disposable(self.cmd_vel.subscribe(self.move)))

        # Start everything in a background thread so we don't block
        # the dimos worker startup (which has a 5-second timeout)
        self._startup_thread = threading.Thread(
            target=self._deferred_start, daemon=True, name="ohmni-startup"
        )
        self._startup_thread.start()

        logger.info("OhmniConnection starting (ip=%s)...", self._config.ip)

    def _deferred_start(self) -> None:
        """Connect to robot and start all subsystems in the background."""
        # Connect to robot — ADB + bot_shell
        self._bridge.connect()

        # Wait for ADB and bot_shell to be ready
        for attempt in range(15):
            if self._bridge.is_ready:
                break
            time.sleep(2)
            logger.info("Waiting for bot_shell... (attempt %d)", attempt + 1)

        if not self._bridge.is_ready:
            logger.warning("bot_shell not ready after 30s — streams will start when it connects")

        # Start camera streams
        self._screen_cam.start()
        self._floor_cam.start()

        # Start LiDAR
        self._lidar_reader.start()

        # Start telemetry polling
        self._telemetry.start(
            on_battery=self._on_battery,
            poll_interval=10.0,
        )

        # Publish static camera info periodically
        self._camera_info_thread = threading.Thread(
            target=self._publish_camera_info_loop,
            daemon=True,
            name="ohmni-caminfo",
        )
        self._camera_info_thread.start()

        # Publish initial odometry so downstream planners/explorers have a
        # pose to work with before any cmd_vel arrives. Otherwise the
        # frontier explorer deadlocks: it needs odom to pick a goal, but
        # odom only updates inside move() once a goal has been issued.
        self._odom_thread = threading.Thread(
            target=self._publish_odom_loop,
            daemon=True,
            name="ohmni-odom",
        )
        self._odom_thread.start()

        logger.info("OhmniConnection fully started — all streams active")

    @rpc
    def stop(self) -> None:
        self._screen_cam.stop()
        self._floor_cam.stop()
        self._lidar_reader.stop()
        self._telemetry.stop()
        self._drive.stop()
        self._bridge.disconnect()

        if self._camera_info_thread and self._camera_info_thread.is_alive():
            self._camera_info_thread.join(timeout=1.0)
        if self._odom_thread and self._odom_thread.is_alive():
            self._odom_thread.join(timeout=1.0)

        super().stop()
        logger.info("OhmniConnection stopped")

    # -- Movement --

    @rpc
    def move(self, twist: Twist, duration: float = 0.0) -> bool:
        """Send velocity command to the robot."""
        self._drive.move_twist(twist.linear.x, twist.angular.z)

        # Dead-reckoning odometry update
        now = time.monotonic()
        if self._last_cmd_time > 0:
            dt = now - self._last_cmd_time
            self._odom_theta += twist.angular.z * dt
            self._odom_x += twist.linear.x * math.cos(self._odom_theta) * dt
            self._odom_y += twist.linear.x * math.sin(self._odom_theta) * dt
            self._publish_odom()
        self._last_cmd_time = now
        return True

    @rpc
    @skill
    def drive(self, linear_x: float = 0.0, angular_z: float = 0.0) -> str:
        """Drive the robot.

        - `linear_x` is forward speed in m/s. Positive = forward, negative = backward.
        - `angular_z` is yaw rate in rad/s. Positive = counterclockwise (turn left).
        - Send `drive(0, 0)` to stop. The drive watchdog auto-zeroes if no
          new command arrives within ~0.5s, so call repeatedly to keep moving.
        - Safe range: linear_x ∈ [-0.30, 0.30], angular_z ∈ [-1.0, 1.0].
        """
        from dimos.msgs.geometry_msgs import Twist, Vector3
        twist = Twist(
            linear=Vector3(float(linear_x), 0.0, 0.0),
            angular=Vector3(0.0, 0.0, float(angular_z)),
        )
        ok = self.move(twist)
        return "ok" if ok else "drive failed"

    @rpc
    @skill
    def stop(self) -> str:
        """Immediately stop all motion. Same as `drive(0, 0)`."""
        return self.drive(0.0, 0.0)

    @rpc
    @skill
    def set_neck_angle(self, angle: int) -> str:
        """Tilt the robot's head/neck.

        - `angle` is a 10-bit servo position. Useful values:
          400 = looking down at the floor,
          512 = looking straight ahead (default),
          600 = looking up at the ceiling.
        - Values are clamped to [400, 600].
        - The head must be awake; this method calls `wake_head` automatically
          if needed.
        """
        a = max(400, min(600, int(angle)))
        return self._telemetry.set_neck_angle(a)

    @rpc
    @skill
    def say(self, text: str) -> str:
        """Speak text out loud through the robot's tablet speakers.

        - `text` is what the robot will say. Keep it short — spoken TTS
          takes time and can't be cancelled mid-sentence.
        - Single-quote characters are auto-escaped for shell safety.
        - Use this whenever the robot has something for a nearby human to hear.
        """
        safe = text.replace("'", "\\'")
        return self._bridge.send_command(f"say {safe}")

    @rpc
    @skill
    def wake_head(self) -> str:
        """Power on the neck servo so it responds to `set_neck_angle`.

        Idempotent. Safe to call repeatedly.
        """
        return self._telemetry.wake_head()

    @rpc
    @skill
    def rest_head(self) -> str:
        """Power off the neck servo. Saves a tiny bit of current; the head
        will hold position by friction. Call `wake_head` again before any
        further `set_neck_angle` calls.
        """
        return self._telemetry.rest_head()

    @rpc
    @skill
    def set_led(
        self,
        duration: int = 3000,
        hue: int = 120,
        sat: int = 255,
        val: int = 255,
    ) -> str:
        """Light up the LED ring around the head in HSV color.

        - `duration` is in milliseconds.
        - `hue` is 0-360 (0=red, 60=yellow, 120=green, 180=cyan, 240=blue,
          300=magenta).
        - `sat` and `val` are 0-255 each.
        - Use as a feedback signal — green for "got it", red for "blocked",
          blue for "thinking", etc.
        """
        return self._telemetry.set_led(int(duration), int(hue), int(sat), int(val))

    @rpc
    @skill
    def observe(self) -> Image | None:
        """Return the latest frame from the head-mounted screen camera.

        Use this skill for visual world queries — what does the robot see?
        Returns None until at least one frame has been captured.
        """
        return self._latest_video_frame

    @rpc
    @skill
    def floor_observe(self) -> Image | None:
        """Return the latest frame from the floor camera.

        Use this for tasks involving what's directly in front of and below
        the robot (docking, obstacle ID, finding objects on the floor).
        Returns None until at least one frame has been captured.
        """
        return self._latest_floor_frame

    @rpc
    @skill
    def get_battery(self) -> dict:
        """Return current battery state as `{level, charging, raw}`.

        - `level` is 0–100 (percent).
        - `charging` is True if on the dock.
        - Use this before starting any non-trivial autonomous run, and refuse
          to start motion-heavy tasks if level < 20.
        """
        status = self._telemetry.get_battery()
        return {
            "level": status.level,
            "charging": status.charging,
            "raw": status.raw,
        }

    @rpc
    @skill
    def get_obstacles(self) -> dict:
        """Return the lidar collision-detection node's three-zone obstacle map.

        Returns `{front: int, side: int, back: int}` where each value is
        0=clear, 1=obstacle near, 2=obstacle blocking. Cheaper than a full
        scan; use as a quick pre-check before issuing a `drive` command.
        """
        resp = self._bridge.send_command("lidar_get_obstacles", timeout=1.0)
        line = next(
            (l for l in resp.splitlines() if l.startswith("OBS:")),
            "",
        )
        if not line:
            return {"front": -1, "side": -1, "back": -1}
        parts = line[len("OBS:"):].split(",")
        try:
            zones = [int(p) for p in parts]
        except ValueError:
            return {"front": -1, "side": -1, "back": -1}
        zones += [0] * (3 - len(zones))
        return {"front": zones[0], "side": zones[1], "back": zones[2]}

    @rpc
    @skill
    def get_lidar_scan(self) -> list[dict]:
        """Return the most recent 360° lidar scan as a list of
        `{angle, distance}` dicts.

        - `angle` is degrees, 0=forward, 90=left, 180=back, 270=right.
        - `distance` is millimetres. 0 means "no return" — skip those.
        - Use to inspect the room layout, look for openings, or measure
          distance to a specific direction.
        """
        return list(self._lidar_reader.last_scan or [])

    @rpc
    @skill
    def dock(self) -> str:
        """Drive to and engage the charging dock autonomously.

        Uses the on-device autodock routine: wakes the head, opens the
        camera, recognizes the dock fiducial, and approaches it. Returns
        immediately; check `is_docking()` and `get_battery().charging`
        to know when complete. Best run when battery is low and a clear
        line-of-sight to the dock exists.
        """
        return self._bridge.send_command("autodock", timeout=2.0)

    @rpc
    @skill
    def is_docking(self) -> bool:
        """Return True iff the autodock routine is currently running."""
        resp = self._bridge.send_command("is_autodock_running", timeout=1.0)
        return "true" in resp.lower()

    # -- Stream callbacks --

    def _on_screen_frame(self, frame: np.ndarray) -> None:
        img = Image(data=frame, ts=time.time(), frame_id="camera_optical")
        self.color_image.publish(img)
        self._latest_video_frame = img

    def _on_floor_frame(self, frame: np.ndarray) -> None:
        img = Image(data=frame, ts=time.time(), frame_id="floor_camera")
        self.floor_image.publish(img)
        self._latest_floor_frame = img

    _lidar_scan_count: int = 0
    _lidar_log_count: int = 0

    def _on_lidar_scan(self, scan: list[dict]) -> None:
        """Convert RPLidar scan to PointCloud2 and publish."""
        if not scan:
            return

        # Convert angle/distance to (x, y, 0) in robot frame
        points = np.empty((len(scan), 3), dtype=np.float32)
        for i, r in enumerate(scan):
            angle_rad = math.radians(r["angle"])
            dist_m = r["distance"] / 1000.0
            points[i, 0] = dist_m * math.cos(angle_rad)  # x = forward
            points[i, 1] = dist_m * math.sin(angle_rad)  # y = left
            points[i, 2] = 0.0  # 2D lidar

        # Timestamp required — downstream LCM serializer does int(ts).
        ts = time.time()
        pc = PointCloud2.from_numpy(points, frame_id="base_link", timestamp=ts)
        self.lidar.publish(pc)
        self.pointcloud.publish(pc)

        self._lidar_scan_count += 1
        if self._lidar_scan_count % 25 == 1:
            logger.info(
                "lidar scan #%d published: %d points",
                self._lidar_scan_count, len(points),
            )

    def _on_battery(self, status) -> None:
        self.battery.publish({
            "level": status.level,
            "charging": status.charging,
            "raw": status.raw,
        })

    def _publish_odom(self) -> None:
        """Publish dead-reckoned odometry."""
        half_theta = self._odom_theta / 2.0
        ts = time.time()
        odom = PoseStamped(
            position=Vector3(self._odom_x, self._odom_y, 0.0),
            orientation=Quaternion(
                0.0, 0.0,
                math.sin(half_theta),
                math.cos(half_theta),
            ),
            frame_id="world",
            ts=ts,
        )
        self.odom.publish(odom)

        # Publish TF transforms
        camera_link = Transform(
            translation=Vector3(0.0, 0.0, 1.2),  # Camera ~1.2m above base
            rotation=Quaternion(0.0, 0.0, 0.0, 1.0),
            frame_id="base_link",
            child_frame_id="camera_link",
            ts=odom.ts,
        )
        self.tf.publish(
            Transform.from_pose("base_link", odom),
            camera_link,
        )

    def _publish_camera_info_loop(self) -> None:
        """Publish camera intrinsics periodically."""
        while self._bridge._running:
            self.camera_info.publish(self.camera_info_static)
            time.sleep(1.0)

    # Wheel-odom calibration. Encoder counts per metre of travel and
    # distance between wheels (metres). Tuned via env if drift is bad.
    # The Ohmni 5-2 base has ~6cm wheels — so 2π·0.06 ≈ 0.377 m per
    # revolution. Servo encoder typically reports ~4096 counts/rev.
    # Default APOS_PER_M ≈ 4096 / 0.377 ≈ 10860.
    import os as _os
    APOS_PER_M = float(_os.environ.get("OHMNI_APOS_PER_M", "10860.0"))
    WHEELBASE_M = float(_os.environ.get("OHMNI_WHEELBASE_M", "0.30"))
    del _os

    def _wheel_odom_tick(self) -> None:
        """Poll wheel encoders and integrate into pose.

        Differential drive math:
            d_left  = (apos_left  - prev_left)  / APOS_PER_M
            d_right = (apos_right - prev_right) / APOS_PER_M
            d_center = (d_left + d_right) / 2
            d_theta  = (d_right - d_left) / WHEELBASE_M
            x += d_center * cos(theta + d_theta/2)
            y += d_center * sin(theta + d_theta/2)
            theta += d_theta
        """
        try:
            left, right = self._telemetry.read_wheel_apos()
        except Exception as e:  # noqa: BLE001
            logger.warning("wheel_apos read failed: %s", e)
            return
        if left is None or right is None:
            return
        if self._wheel_left_last is None or self._wheel_right_last is None:
            self._wheel_left_last = left
            self._wheel_right_last = right
            return
        d_left = (left - self._wheel_left_last) / self.APOS_PER_M
        d_right = (right - self._wheel_right_last) / self.APOS_PER_M
        # Right wheel is mirrored (rotates opposite for forward motion);
        # bot_shell already accounts for this in `manual_move`, but the
        # apos signs are still mirrored. Negate right delta to align.
        d_right = -d_right
        d_center = (d_left + d_right) / 2.0
        d_theta = (d_right - d_left) / self.WHEELBASE_M
        mid_theta = self._odom_theta + d_theta / 2.0
        self._odom_x += d_center * math.cos(mid_theta)
        self._odom_y += d_center * math.sin(mid_theta)
        self._odom_theta += d_theta
        self._wheel_left_last = left
        self._wheel_right_last = right

    def _publish_odom_loop(self) -> None:
        """Publish odometry at a fixed rate.

        Uses wheel-encoder readings via `apos` when available, falling back
        to the dead-reckoned integration in `move()` when they fail.
        """
        # Stagger wheel polls — apos queries are ~50-100ms each round-trip,
        # so don't poll every 200ms publish cycle.
        last_wheel_poll = 0.0
        WHEEL_POLL_INTERVAL = 0.5
        while self._bridge._running:
            now = time.monotonic()
            if now - last_wheel_poll >= WHEEL_POLL_INTERVAL:
                last_wheel_poll = now
                self._wheel_odom_tick()
            self._publish_odom()
            time.sleep(0.2)


# Blueprint factory
ohmni_connection = OhmniConnection.blueprint
