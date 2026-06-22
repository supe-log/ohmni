"""SemanticPin — drop VLM-derived class labels onto the world frame.

Subscribes to color_image + odom. Periodically (default 0.5 Hz) sends
the current frame to a VLM and asks one question: "what's the dominant
object in front of the robot?" The reply is associated with the robot's
current pose and appended to ~/.ohmni/world.json.

Over time this builds a sparse semantic map: pose → label. The brain
uses it to navigate by name ("go to where you saw the kitchen").

Honest implementation note: the *VLM call* is deferred behind a
pluggable function (`_describe_frame`) so this module works without an
LLM provider. The default stub returns "unknown"; when the agent's VLM
is available, swap in the real call.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from reactivex.disposable import Disposable

from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In
from dimos.msgs.geometry_msgs import PoseStamped
from dimos.msgs.sensor_msgs import Image
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


def _stub_describe_frame(img: Image) -> str:
    """Default describe-fn: no VLM call, just return 'unknown'.

    Replace by swapping `SemanticPinConfig.describe_fn` with a real
    VLM-backed callable when the agent stack is live.
    """
    return "unknown"


def _make_vlm_describe_fn(model_name: str = "qwen") -> Callable[[Image], str]:
    """Build a describe-fn that delegates to a dimos VlModel.

    Lazily constructed — VL model deps are heavy (qwen ~10GB) so we
    only build this if explicitly requested. Caches the model.
    """
    cache: dict = {}

    def describe(img: Image) -> str:
        try:
            if "model" not in cache:
                from dimos.models.vl.create import create
                cache["model"] = create(model_name)
            label = cache["model"].caption(img)
            # Aggressive trim: we want a noun phrase, not a sentence.
            label = label.strip().split("\n")[0]
            if len(label) > 80:
                label = label[:80].rsplit(" ", 1)[0]
            return label
        except Exception as e:  # noqa: BLE001
            logger.warning("vlm caption failed: %s", e)
            return ""

    return describe


@dataclass
class SemanticPinConfig(ModuleConfig):
    world_path: Path = field(default_factory=lambda: Path.home() / ".ohmni" / "world.json")
    sample_interval_s: float = 2.0
    min_pose_delta_m: float = 0.5
    describe_fn: Callable[[Image], str] = field(default=_stub_describe_frame)
    # If non-empty, build a VLM-backed describe_fn at start using this
    # model name (one of "qwen", "moondream"). Overrides describe_fn.
    vlm_model: str = ""


class SemanticPin(Module):
    """Pins VLM labels onto poses on the world map."""

    default_config = SemanticPinConfig
    config: SemanticPinConfig

    color_image: In[Image]
    odom: In[PoseStamped]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.config.world_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config.vlm_model:
            self.config.describe_fn = _make_vlm_describe_fn(self.config.vlm_model)
        self._latest_img: Image | None = None
        self._latest_odom: PoseStamped | None = None
        self._last_sample_ts: float = 0.0
        self._last_pinned_pose: tuple[float, float] | None = None
        self._lock = threading.Lock()
        self._loop_thread: threading.Thread | None = None
        self._running = False

    @rpc
    def start(self) -> None:
        super().start()
        self._disposables.add(
            Disposable(self.color_image.subscribe(self._on_image))
        )
        self._disposables.add(Disposable(self.odom.subscribe(self._on_odom)))
        self._running = True
        self._loop_thread = threading.Thread(
            target=self._sample_loop, daemon=True, name="semantic-pin"
        )
        self._loop_thread.start()
        logger.info("SemanticPin running. World map at %s", self.config.world_path)

    @rpc
    def stop(self) -> None:
        self._running = False
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=2.0)
        super().stop()

    def _on_image(self, img: Image) -> None:
        self._latest_img = img

    def _on_odom(self, pose: PoseStamped) -> None:
        self._latest_odom = pose

    def _sample_loop(self) -> None:
        while self._running:
            time.sleep(self.config.sample_interval_s)
            try:
                self._sample()
            except Exception as e:  # noqa: BLE001
                logger.warning("semantic_pin sample error: %s", e)

    def _sample(self) -> None:
        if self._latest_img is None or self._latest_odom is None:
            return
        pose = self._latest_odom
        xy = (pose.position.x, pose.position.y)
        # Only pin when the robot has actually moved enough since last pin.
        if self._last_pinned_pose is not None:
            d = math.hypot(
                xy[0] - self._last_pinned_pose[0],
                xy[1] - self._last_pinned_pose[1],
            )
            if d < self.config.min_pose_delta_m:
                return

        label = self.config.describe_fn(self._latest_img)
        if not label or label == "unknown":
            return

        entry = {
            "ts": time.time(),
            "x": float(xy[0]),
            "y": float(xy[1]),
            "label": label,
        }
        self._append(entry)
        self._last_pinned_pose = xy

    def _append(self, entry: dict) -> None:
        # Append-only JSONL.
        try:
            with self.config.world_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning("world_path write failed: %s", e)

    @rpc
    @skill
    def query_label(self, label: str) -> list[dict]:
        """Find all pinned (x, y) where the recorded label contains `label`.

        Use this to answer "where is the kitchen?" — returns a list of
        pose entries in chronological order.
        """
        path = self.config.world_path
        if not path.exists():
            return []
        results: list[dict] = []
        try:
            with path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if label.lower() in str(entry.get("label", "")).lower():
                        results.append(entry)
        except OSError:
            return []
        return results

    @rpc
    @skill
    def list_pinned_labels(self) -> list[str]:
        """Return the unique label set that has been pinned to the world."""
        path = self.config.world_path
        if not path.exists():
            return []
        labels: set[str] = set()
        try:
            with path.open("r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    lbl = entry.get("label")
                    if lbl:
                        labels.add(str(lbl))
        except OSError:
            pass
        return sorted(labels)


semantic_pin = SemanticPin.blueprint
