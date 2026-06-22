"""SafetyGovernor — sole consumer of cmd_vel before it reaches the drive.

Intercepts twist commands from any source (planner, agent, teleop) and:
- zero-clamps when an obstacle is within `imminent_collision_m` in the
  direction of motion,
- caps linear speed when an obstacle is within `approach_warning_m`,
- enforces global speed and angular-rate ceilings,
- holds emergency-latch on imminent collision until ack'd,
- cancels motion when battery < `battery_low_pct`,
- detects "stuck" (cmd non-zero but odom delta < threshold for `stuck_s`),
- enforces a geofence around `home_pose`.

Wiring (in a blueprint):
    autoconnect(
        ohmni_smart.remap(cmd_vel=Remap(planner='raw_cmd_vel')),
        safety_governor(),
    )

Stream contract:
    raw_cmd_vel: In[Twist]      -- agent / planner output
    pointcloud:  In[PointCloud2]-- 360° lidar in robot frame
    battery:     In[dict]       -- {level: 0-100, charging: bool}
    odom:        In[PoseStamped]
    cmd_vel:     Out[Twist]     -- the clamped twist OhmniConnection consumes
    emergency:   Out[bool]      -- latched on imminent-collision
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from reactivex.disposable import Disposable

from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs import PoseStamped, Twist, Vector3
from dimos.msgs.sensor_msgs import PointCloud2
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


@dataclass
class SafetyConfig(ModuleConfig):
    """All adjustable safety parameters for the governor."""

    # Speed caps
    max_linear_mps: float = 0.30
    max_angular_rps: float = 1.0

    # Distance thresholds (metres)
    imminent_collision_m: float = 0.30
    approach_warning_m: float = 0.60
    approach_speed_cap_mps: float = 0.10

    # Auto-clear the imminent-collision latch if the cone has been clear
    # of obstacles for this many seconds *and* the incoming command is
    # not pushing forward. Set to 0 to disable auto-clear (a human must
    # call clear_emergency()).
    auto_clear_seconds: float = 3.0

    # Front detection cone (degrees half-width either side of motion direction)
    cone_half_deg: float = 45.0

    # Battery thresholds
    battery_low_pct: float = 15.0
    battery_resume_pct: float = 30.0

    # Stuck detection
    stuck_window_s: float = 5.0
    stuck_motion_threshold_m: float = 0.05

    # Geofence
    geofence_radius_m: float = 8.0
    home_pose: tuple[float, float] = (0.0, 0.0)

    # Watchdog
    cmd_timeout_s: float = 0.6


class SafetyGovernor(Module):
    """Single Twist consumer for OhmniConnection. Clamps, gates, and republishes."""

    default_config = SafetyConfig
    config: SafetyConfig

    raw_cmd_vel: In[Twist]
    pointcloud: In[PointCloud2]
    battery: In[dict]
    odom: In[PoseStamped]

    cmd_vel: Out[Twist]
    emergency: Out[bool]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._latest_pc: np.ndarray | None = None
        self._latest_pc_ts: float = 0.0
        self._latest_battery: dict = {"level": 100.0, "charging": False}
        self._latest_odom: PoseStamped | None = None
        self._last_cmd_ts: float = 0.0
        self._last_motion_ts: float = 0.0
        self._last_motion_pose: tuple[float, float] | None = None
        self._emergency_latched: bool = False
        self._battery_lockout: bool = False
        self._lock = threading.Lock()
        self._latch_ts: float = 0.0
        self._cone_clear_since: float = 0.0

    @rpc
    def start(self) -> None:
        super().start()
        self._disposables.add(
            Disposable(self.raw_cmd_vel.subscribe(self._on_raw_cmd))
        )
        self._disposables.add(
            Disposable(self.pointcloud.subscribe(self._on_pointcloud))
        )
        self._disposables.add(Disposable(self.battery.subscribe(self._on_battery)))
        self._disposables.add(Disposable(self.odom.subscribe(self._on_odom)))

        # Watchdog thread: re-publish zero if no command in window
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="safety-watchdog"
        )
        self._watchdog_running = True
        self._watchdog_thread.start()

        logger.info(
            "SafetyGovernor active. v_max=%.2f w_max=%.2f imminent=%.2fm",
            self.config.max_linear_mps,
            self.config.max_angular_rps,
            self.config.imminent_collision_m,
        )

    @rpc
    def stop(self) -> None:
        self._watchdog_running = False
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=1.0)
        # Always end with a zero twist
        self._publish_twist(0.0, 0.0)
        super().stop()

    # -- input handlers --

    def _on_pointcloud(self, pc: PointCloud2) -> None:
        try:
            arr, _ = pc.as_numpy()
            self._latest_pc = arr
            self._latest_pc_ts = time.monotonic()
        except Exception as e:  # noqa: BLE001
            logger.warning("safety: failed to ingest pointcloud: %s", e)

    def _on_battery(self, status: dict) -> None:
        self._latest_battery = status or {"level": 100.0, "charging": False}
        level = float(status.get("level", 100.0))
        if level < self.config.battery_low_pct and not self._battery_lockout:
            self._battery_lockout = True
            logger.warning("safety: battery %.1f%% — locking out motion", level)
        elif level > self.config.battery_resume_pct and self._battery_lockout:
            self._battery_lockout = False
            logger.info("safety: battery %.1f%% — motion re-enabled", level)

    def _on_odom(self, pose: PoseStamped) -> None:
        self._latest_odom = pose

    _raw_cmd_count: int = 0

    def _on_raw_cmd(self, twist: Twist) -> None:
        with self._lock:
            self._last_cmd_ts = time.monotonic()
            lin, ang = self._gate(twist.linear.x, twist.angular.z)
            self._publish_twist(lin, ang)
            self._raw_cmd_count += 1
            if self._raw_cmd_count <= 5 or self._raw_cmd_count % 50 == 0:
                logger.info(
                    "safety gate #%d: in=(%.3f,%.3f) out=(%.3f,%.3f)",
                    self._raw_cmd_count,
                    float(twist.linear.x), float(twist.angular.z),
                    float(lin), float(ang),
                )

    # -- core gating --

    def _gate(self, lin: float, ang: float) -> tuple[float, float]:
        cfg = self.config
        now = time.monotonic()

        # Auto-clear emergency latch when the imminent zone has been
        # clear for `auto_clear_seconds` AND the requested command is
        # not pushing into the originally-blocked direction. This lets
        # the planner replan around an obstacle without a human in the
        # loop.
        if self._emergency_latched and cfg.auto_clear_seconds > 0:
            nearest = self._nearest_in_motion_cone(lin if abs(lin) > 1e-3 else 0.1)
            cone_clear = nearest is None or nearest > cfg.imminent_collision_m * 1.5
            if cone_clear:
                if self._cone_clear_since == 0.0:
                    self._cone_clear_since = now
                elif now - self._cone_clear_since >= cfg.auto_clear_seconds:
                    logger.info(
                        "safety: cone clear for %.1fs — releasing emergency latch",
                        cfg.auto_clear_seconds,
                    )
                    self._emergency_latched = False
                    self._cone_clear_since = 0.0
                    self.emergency.publish(False)
            else:
                self._cone_clear_since = 0.0

        if self._emergency_latched:
            return 0.0, 0.0
        if self._battery_lockout:
            return 0.0, 0.0

        # Geofence
        if self._latest_odom is not None:
            dx = self._latest_odom.position.x - cfg.home_pose[0]
            dy = self._latest_odom.position.y - cfg.home_pose[1]
            if math.hypot(dx, dy) > cfg.geofence_radius_m and lin > 0:
                # Outside fence and trying to go further forward — block
                logger.warning("safety: geofence breach, refusing forward")
                lin = min(lin, 0.0)

        # Lidar-based forward gating
        nearest = self._nearest_in_motion_cone(lin)
        if nearest is not None:
            if nearest <= cfg.imminent_collision_m and lin > 0:
                logger.error(
                    "safety: imminent collision at %.2fm — STOP & latch", nearest
                )
                self._emergency_latched = True
                self._latch_ts = now
                self._cone_clear_since = 0.0
                self.emergency.publish(True)
                return 0.0, 0.0
            if nearest <= cfg.approach_warning_m and lin > 0:
                lin = min(lin, cfg.approach_speed_cap_mps)

        # Hard speed clamps
        lin = max(-cfg.max_linear_mps, min(cfg.max_linear_mps, lin))
        ang = max(-cfg.max_angular_rps, min(cfg.max_angular_rps, ang))

        # Stuck detection — only updates state, doesn't gate alone
        self._update_stuck(lin, ang)

        return lin, ang

    def _nearest_in_motion_cone(self, lin: float) -> float | None:
        """Smallest distance among lidar points inside the forward (or
        backward) motion cone. Returns None if no fresh data."""
        pc = self._latest_pc
        if pc is None or pc.shape[0] == 0:
            return None
        if time.monotonic() - self._latest_pc_ts > 1.0:
            return None
        if abs(lin) < 1e-3:
            return None

        # Cone heading: forward (0°) when lin>0, back (180°) when lin<0
        heading = 0.0 if lin > 0 else math.pi
        half = math.radians(self.config.cone_half_deg)

        x = pc[:, 0]
        y = pc[:, 1]
        # Pre-compute distance and drop invalid (zero / sub-cm) returns —
        # those come from lidar filter passes that map "no return" to
        # exactly 0, which would otherwise trip imminent-collision.
        dist_all = np.sqrt(x * x + y * y)
        valid = dist_all > 0.05  # 5cm minimum
        if not np.any(valid):
            return None
        x = x[valid]; y = y[valid]; dist_all = dist_all[valid]
        ang = np.arctan2(y, x)
        # Wrap angles relative to heading into [-pi, pi]
        delta = (ang - heading + math.pi) % (2 * math.pi) - math.pi
        in_cone = np.abs(delta) <= half
        if not np.any(in_cone):
            return None
        dists = dist_all[in_cone]
        return float(np.min(dists)) if dists.size else None

    def _update_stuck(self, lin: float, ang: float) -> None:
        if abs(lin) < 1e-3 and abs(ang) < 1e-3:
            self._last_motion_pose = None
            return
        if self._latest_odom is None:
            return
        now = time.monotonic()
        pose = (self._latest_odom.position.x, self._latest_odom.position.y)
        if self._last_motion_pose is None:
            self._last_motion_pose = pose
            self._last_motion_ts = now
            return
        if now - self._last_motion_ts >= self.config.stuck_window_s:
            d = math.hypot(
                pose[0] - self._last_motion_pose[0],
                pose[1] - self._last_motion_pose[1],
            )
            if d < self.config.stuck_motion_threshold_m:
                logger.warning(
                    "safety: stuck (Δ=%.2fm in %.1fs) — backing off",
                    d, self.config.stuck_window_s,
                )
            self._last_motion_pose = pose
            self._last_motion_ts = now

    def _publish_twist(self, lin: float, ang: float) -> None:
        twist = Twist(
            linear=Vector3(float(lin), 0.0, 0.0),
            angular=Vector3(0.0, 0.0, float(ang)),
        )
        self.cmd_vel.publish(twist)

    def _watchdog_loop(self) -> None:
        """Auto-zero if no fresh raw_cmd_vel for cmd_timeout_s.

        Only fires once per "we previously had a non-stale command and
        now it is stale" transition. We don't want to broadcast zeros
        every 200ms forever — that drowns out the planner's own
        cmd_vel publishes. Once we publish a watchdog stop, we wait
        for the next raw_cmd_vel before re-arming.
        """
        watchdog_armed = False
        while self._watchdog_running:
            time.sleep(0.2)
            if self._last_cmd_ts == 0.0:
                continue  # Never received a command — nothing to time out
            now = time.monotonic()
            stale = now - self._last_cmd_ts > self.config.cmd_timeout_s
            if stale and watchdog_armed:
                self._publish_twist(0.0, 0.0)
                watchdog_armed = False
            elif not stale:
                watchdog_armed = True

    # -- skills --

    @rpc
    @skill
    def clear_emergency(self) -> str:
        """Clear the emergency latch after a hard stop. Use only when the
        path is verified clear by a human or by a fresh lidar scan."""
        self._emergency_latched = False
        self.emergency.publish(False)
        return "emergency cleared"

    @rpc
    @skill
    def set_geofence(self, radius_m: float) -> str:
        """Update the geofence radius (metres) at runtime."""
        self.config.geofence_radius_m = max(0.5, float(radius_m))
        return f"geofence={self.config.geofence_radius_m}"

    @rpc
    @skill
    def set_speed_cap(self, max_linear_mps: float, max_angular_rps: float) -> str:
        """Update speed and angular-rate caps at runtime. Caps are clamped to
        sane absolute limits (0.5 m/s, 1.5 rad/s)."""
        self.config.max_linear_mps = max(0.0, min(0.5, float(max_linear_mps)))
        self.config.max_angular_rps = max(0.0, min(1.5, float(max_angular_rps)))
        return f"v_max={self.config.max_linear_mps} w_max={self.config.max_angular_rps}"

    @rpc
    def status(self) -> dict:
        """Diagnostic snapshot of the governor's internal state."""
        return {
            "emergency_latched": self._emergency_latched,
            "battery_lockout": self._battery_lockout,
            "battery_level": self._latest_battery.get("level"),
            "have_pc": self._latest_pc is not None,
            "have_odom": self._latest_odom is not None,
        }


safety_governor = SafetyGovernor.blueprint
