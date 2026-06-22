"""Audio I/O for the Ohmni robot.

Stage 1 approach:
- Speaker output via bot_shell `say <text>` (Android TTS)
- Mic input via `adb exec-out tinycap` if available, else host mic fallback

Dimos audio system uses AbstractAudioEmitter/AbstractAudioConsumer with
RxPY Observable[AudioEvent] — not the Module In/Out stream pattern.
"""

import logging
import subprocess
import threading
import time

import numpy as np

from .bridge import OhmniBridge

logger = logging.getLogger(__name__)


class OhmniSpeaker:
    """Play audio/speech on the Ohmni robot.

    Stage 1: Uses bot_shell `say` command (Android TTS).
    Future: Stream PCM audio via ADB tinyplay.
    """

    def __init__(self, bridge: OhmniBridge) -> None:
        self._bridge = bridge

    def say(self, text: str) -> str:
        """Speak text via Android TTS."""
        # Escape single quotes for shell safety
        safe_text = text.replace("'", "\\'")
        return self._bridge.send_command(f"say {safe_text}")

    def play_file(self, device_path: str) -> None:
        """Play an audio file on the robot via Android intent.

        The file must already exist on the device (e.g. pushed via ADB).
        """
        subprocess.Popen(
            [
                "adb", "-s", self._bridge._adb_addr, "shell",
                "am", "start", "-a", "android.intent.action.VIEW",
                "-d", f"file://{device_path}",
                "-t", "audio/mpeg",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop_playback(self) -> None:
        """Stop any playing audio on the robot."""
        addr = self._bridge._adb_addr
        subprocess.Popen(
            ["adb", "-s", addr, "shell", "input", "keyevent", "86"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class OhmniMicrophone:
    """Capture audio from the Ohmni's microphone via ADB.

    Requires `tinycap` on the device (verified during Stage 0 archive).
    Falls back gracefully if unavailable.
    """

    def __init__(self, adb_addr: str, sample_rate: int = 16000) -> None:
        self._adb_addr = adb_addr
        self._sample_rate = sample_rate
        self._process: subprocess.Popen | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._on_audio: "callable | None" = None
        self._available: bool | None = None  # None = not checked yet

    def check_available(self) -> bool:
        """Check if tinycap exists on the device."""
        try:
            result = subprocess.run(
                ["adb", "-s", self._adb_addr, "shell", "which", "tinycap"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            self._available = result.returncode == 0 and "tinycap" in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            self._available = False
        return self._available

    def start(self, on_audio: "callable") -> bool:
        """Start streaming audio. Returns False if tinycap not available."""
        if self._available is None:
            self.check_available()
        if not self._available:
            logger.warning(
                "tinycap not available on device — mic capture disabled. "
                "Use host mic via dimos SounddeviceAudioSource as fallback."
            )
            return False

        self._on_audio = on_audio
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="ohmni-mic"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._process:
            try:
                self._process.kill()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _capture_loop(self) -> None:
        while self._running:
            try:
                self._run_capture()
            except Exception as e:
                logger.warning("Mic capture error: %s", e)
            if self._running:
                time.sleep(2)

    def _run_capture(self) -> None:
        # tinycap captures raw PCM from ALSA
        self._process = subprocess.Popen(
            [
                "adb", "-s", self._adb_addr, "exec-out",
                "tinycap", "/dev/stdin",
                "-D", "0", "-d", "0",
                "-r", str(self._sample_rate),
                "-b", "16", "-c", "1",
                "-p", "1024",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        chunk_size = self._sample_rate * 2  # 1 second of 16-bit mono
        try:
            while self._running and self._process.poll() is None:
                data = self._process.stdout.read(chunk_size)
                if not data:
                    break
                # Convert raw PCM to numpy
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                samples /= 32768.0  # Normalize to [-1, 1]
                if self._on_audio:
                    self._on_audio(samples, self._sample_rate)
        finally:
            if self._process:
                try:
                    self._process.kill()
                except OSError:
                    pass
                self._process = None
