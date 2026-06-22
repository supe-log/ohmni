#!/usr/bin/env python3
"""One-shot trigger: tell the running ohmni_full coordinator's
WavefrontFrontierExplorer to start.

This runs against an already-booted blueprint via the bot_shell-side
explore_cmd LCM topic. Publishing Bool(true) to /explore_cmd kicks the
explorer's `explore()` method through its subscribed handler.
"""

import os
import sys
import time

os.environ.setdefault("ANDROID_ADB_SERVER_PORT", "6037")
os.environ["CI"] = "1"

from dimos.core.transport import LCMTransport
from dimos.core.global_config import global_config
from dimos_lcm.std_msgs import Bool


def main() -> None:
    global_config.update(robot_ip="192.168.1.194")
    print("Publishing Bool(true) to /explore_cmd...")
    t = LCMTransport("explore_cmd", Bool)
    t.publish(Bool(data=True))
    time.sleep(0.5)
    print("Done. Frontier explorer should now be running.")


if __name__ == "__main__":
    main()
