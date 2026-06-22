"""MJPEG camera capture via ADB + v4l2-ctl.

Port of control-app/server.js lines 263-410:
- Spawns `adb exec-out v4l2-ctl --stream-mmap --stream-to=-` subprocess
- Parses continuous MJPEG byte stream
- Extracts JPEG frames by SOI/EOI markers (0xFFD8/0xFFD9)
- Decodes to numpy arrays for dimos Image messages
"""

import logging
import subprocess
import threading
import time

import cv2
import numpy as np

from .bridge import CameraDevice, OhmniBridge

logger = logging.getLogger(__name__)

# JPEG markers
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class OhmniCameraStream:
    """Continuous MJPEG capture from an Ohmni camera via ADB.

    Spawns a persistent `adb exec-out v4l2-ctl` process that streams
    MJPEG frames. Each complete JPEG is decoded and provided to a callback.
    Auto-restarts on process exit if still running.
    """

    def __init__(
        self,
        bridge: OhmniBridge,
        cam_id: int = 1,
        on_frame: "callable | None" = None,
    ) -> None:
        self._bridge = bridge
        self._cam_id = cam_id
        self._on_frame = on_frame
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._latest_frame: np.ndarray | None = None
        self._frame_count = 0

    @property
    def latest_frame(self) -> np.ndarray | None:
        return self._latest_frame

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name=f"ohmni-cam-{self._cam_id}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._process:
            try:
                self._process.kill()
            except OSError:
                pass
            self._process = None
        if self._thread:
            self._thread.join(timeout=3)

    def capture_single(self) -> np.ndarray | None:
        """Capture a single frame (blocking). For one-shot VLM queries."""
        cam = self._bridge.cameras.get(self._cam_id)
        if not cam:
            logger.warning("Camera %d not detected", self._cam_id)
            return None

        w, h = cam.resolution.split("x")
        try:
            result = subprocess.run(
                [
                    "adb", "-s", self._bridge._adb_addr, "exec-out",
                    "v4l2-ctl", f"--device={cam.dev}",
                    f"--set-fmt-video=width={w},height={h},pixelformat=MJPG",
                    "--stream-mmap", "--stream-count=1", "--stream-to=-",
                ],
                capture_output=True,
                timeout=5,
            )
            jpeg = _extract_jpeg(result.stdout)
            if jpeg:
                arr = np.frombuffer(jpeg, dtype=np.uint8)
                return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning("Single capture failed: %s", e)
        return None

    def _capture_loop(self) -> None:
        """Capture frames via periodic single-shot captures.

        WiFi ADB mangles continuous MJPEG streams (byte loss), so we use
        individual v4l2-ctl --stream-count=1 calls which complete reliably.
        Yields ~1-2 FPS which is sufficient for VLMs and the command center.
        """
        consecutive_failures = 0
        while self._running:
            cam = self._bridge.cameras.get(self._cam_id)
            if not cam:
                self._bridge.detect_cameras()
                time.sleep(2)
                continue

            frame = self._capture_single_from(cam)
            if frame is not None:
                self._latest_frame = frame
                self._frame_count += 1
                consecutive_failures = 0
                if self._on_frame:
                    try:
                        self._on_frame(frame)
                    except Exception as e:
                        logger.warning("Frame callback error: %s", e)
            else:
                consecutive_failures += 1

            # Re-detect cameras after repeated failures (USB re-enumeration)
            if consecutive_failures >= 5:
                logger.info("Camera %d: re-detecting after %d failures",
                            self._cam_id, consecutive_failures)
                self._bridge.detect_cameras()
                consecutive_failures = 0

            # ~1 FPS target — the capture itself takes ~0.5-1s over WiFi
            time.sleep(0.3)

    def _capture_single_from(self, cam: CameraDevice) -> "np.ndarray | None":
        """Capture one frame from a known camera device."""
        w, h = cam.resolution.split("x")
        try:
            result = subprocess.run(
                [
                    "adb", "-s", self._bridge._adb_addr, "exec-out",
                    "v4l2-ctl", f"--device={cam.dev}",
                    f"--set-fmt-video=width={w},height={h},pixelformat=MJPG",
                    "--stream-mmap", "--stream-count=1", "--stream-to=-",
                ],
                capture_output=True,
                timeout=5,
            )
            jpeg = _extract_jpeg(result.stdout)
            if jpeg:
                arr = np.frombuffer(jpeg, dtype=np.uint8)
                return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _run_stream(self, cam: CameraDevice) -> None:
        """Run one v4l2-ctl stream session."""
        w, h = cam.resolution.split("x")
        self._process = subprocess.Popen(
            [
                "adb", "-s", self._bridge._adb_addr, "exec-out",
                "v4l2-ctl", f"--device={cam.dev}",
                f"--set-fmt-video=width={w},height={h},pixelformat=MJPG",
                "--stream-mmap", "--stream-count=0", "--stream-to=-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        buf = bytearray()
        try:
            while self._running and self._process.poll() is None:
                chunk = self._process.stdout.read(8192)
                if not chunk:
                    break
                buf.extend(chunk)

                # Extract all complete JPEG frames from buffer
                while True:
                    start = buf.find(JPEG_SOI)
                    if start < 0:
                        break
                    end = buf.find(JPEG_EOI, start + 2)
                    if end < 0:
                        break
                    end += 2  # Include the EOI marker

                    jpeg_data = bytes(buf[start:end])
                    buf = buf[end:]

                    # Decode JPEG to numpy array
                    arr = np.frombuffer(jpeg_data, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        self._latest_frame = frame
                        self._frame_count += 1
                        if self._on_frame:
                            try:
                                self._on_frame(frame)
                            except Exception as e:
                                logger.warning("Frame callback error: %s", e)

                # Prevent buffer from growing unbounded
                if len(buf) > 1_000_000:
                    # Keep only the last 100KB
                    buf = buf[-100_000:]

        finally:
            if self._process:
                try:
                    self._process.kill()
                except OSError:
                    pass
                self._process = None


def _extract_jpeg(data: bytes) -> bytes | None:
    """Extract the first complete JPEG from a byte buffer."""
    start = data.find(JPEG_SOI)
    if start < 0:
        return None
    end = data.find(JPEG_EOI, start + 2)
    if end < 0:
        return None
    return data[start : end + 2]
