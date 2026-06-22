"""Basic Ohmni blueprint: connection + visualization."""

import platform

from dimos.core.blueprints import autoconnect
from dimos.core.transport import pSHMTransport
from dimos.msgs.sensor_msgs import Image
from dimos.web.websocket_vis.websocket_vis_module import websocket_vis

from dimos_ohmni.connection import ohmni_connection

# macOS needs pSHMTransport for high-bandwidth image streams
_mac_transports = {
    ("color_image", Image): pSHMTransport("color_image"),
    ("floor_image", Image): pSHMTransport("floor_image"),
}

_transports_base = (
    autoconnect()
    if platform.system() == "Linux"
    else autoconnect().transports(_mac_transports)
)

ohmni_basic = (
    autoconnect(
        _transports_base,
        ohmni_connection(),
        websocket_vis(),
    )
    .global_config(n_workers=4, robot_model="ohmni_52")
)
