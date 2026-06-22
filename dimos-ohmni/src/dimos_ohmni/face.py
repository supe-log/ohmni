"""Face rendering on the Ohmni tablet display.

Launches Chrome kiosk mode on the Ohmni's Android tablet, pointing
at a web server on the host that renders animated face expressions
driven by WebSocket messages from the dimos agent.

The OhmniLabs app stays running in background (maintains bot_shell.sock);
Chrome just comes to the foreground over it.
"""

import json
import logging
import subprocess
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from .bridge import OhmniBridge

logger = logging.getLogger(__name__)

# Minimal face HTML — SVG animated eyes + mouth driven by JS WebSocket
FACE_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * { margin: 0; padding: 0; }
  body { background: #000; display: flex; align-items: center;
         justify-content: center; height: 100vh; overflow: hidden; }
  svg { width: 80vw; max-width: 600px; }
  .eye { fill: #4fc3f7; transition: all 0.3s; }
  .mouth { fill: none; stroke: #4fc3f7; stroke-width: 4;
           stroke-linecap: round; transition: all 0.3s; }
  .blink .eye { ry: 2; }
  .speaking .mouth { stroke: #81d4fa; }
</style>
</head>
<body>
<svg viewBox="0 0 300 200" id="face">
  <!-- Left eye -->
  <ellipse class="eye" id="leye" cx="100" cy="80" rx="28" ry="32"/>
  <!-- Right eye -->
  <ellipse class="eye" id="reye" cx="200" cy="80" rx="28" ry="32"/>
  <!-- Mouth -->
  <path class="mouth" id="mouth" d="M110,150 Q150,160 190,150"/>
</svg>
<script>
const face = document.getElementById('face');
const mouth = document.getElementById('mouth');
let ws;

function connect() {
  const host = location.hostname || 'localhost';
  ws = new WebSocket('ws://' + host + ':8081/face-ws');
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.expression === 'speaking') {
      face.classList.add('speaking');
      mouth.setAttribute('d', 'M110,145 Q150,175 190,145');
    } else if (msg.expression === 'listening') {
      face.classList.remove('speaking');
      mouth.setAttribute('d', 'M110,155 Q150,155 190,155');
    } else {
      face.classList.remove('speaking');
      mouth.setAttribute('d', 'M110,150 Q150,160 190,150');
    }
    if (msg.text) {
      // Could display subtitle text
    }
  };
  ws.onclose = () => setTimeout(connect, 2000);
  ws.onerror = () => ws.close();
}

// Blink every 3-6 seconds
setInterval(() => {
  face.classList.add('blink');
  setTimeout(() => face.classList.remove('blink'), 200);
}, 3000 + Math.random() * 3000);

connect();
</script>
</body>
</html>
"""


class OhmniFace:
    """Controls the face display on the Ohmni's tablet screen.

    Serves a face HTML page on the host, launches Chrome kiosk on the
    tablet to display it, and sends expression updates via WebSocket.
    """

    def __init__(self, bridge: OhmniBridge, host_port: int = 8080) -> None:
        self._bridge = bridge
        self._host_port = host_port
        self._ws_port = 8081
        self._server: HTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._ws_clients: list = []

    def start(self, host_ip: str | None = None) -> None:
        """Start the face server and launch Chrome on the tablet."""
        # Start HTTP server for the face page
        self._start_server()

        if not host_ip:
            host_ip = self._detect_host_ip()

        # Launch Chrome kiosk on the tablet
        url = f"http://{host_ip}:{self._host_port}/face"
        self._launch_chrome(url)
        logger.info("Face launched at %s", url)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    def set_expression(self, expression: str, text: str = "") -> None:
        """Update the face expression.

        Args:
            expression: "idle", "speaking", "listening", "thinking"
            text: Optional subtitle text to display
        """
        # For Stage 1, this is a placeholder — WebSocket server not yet wired.
        # The face HTML includes a WebSocket client that reconnects.
        logger.debug("Face expression: %s %s", expression, text)

    def _start_server(self) -> None:
        class FaceHandler(SimpleHTTPRequestHandler):
            def do_GET(self_handler):
                if self_handler.path in ("/face", "/face/", "/"):
                    self_handler.send_response(200)
                    self_handler.send_header("Content-Type", "text/html")
                    self_handler.end_headers()
                    self_handler.wfile.write(FACE_HTML.encode())
                else:
                    self_handler.send_error(404)

            def log_message(self_handler, format, *args):
                pass  # Suppress request logs

        self._server = HTTPServer(("0.0.0.0", self._host_port), FaceHandler)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="ohmni-face-http"
        )
        self._server_thread.start()

    def _launch_chrome(self, url: str) -> None:
        """Launch Chrome in kiosk mode on the Ohmni tablet."""
        addr = self._bridge._adb_addr
        # Try Chrome first, fall back to default browser
        for activity in [
            "com.android.chrome/com.google.android.apps.chrome.Main",
            None,  # Default browser via intent
        ]:
            args = [
                "adb", "-s", addr, "shell", "am", "start",
                "-a", "android.intent.action.VIEW",
                "-d", url,
            ]
            if activity:
                args.extend(["-n", activity])
            args.append("--activity-clear-top")

            result = subprocess.run(
                args, capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and "Error" not in result.stdout:
                return

        logger.warning("Could not launch browser on tablet")

    def _detect_host_ip(self) -> str:
        """Detect this host's LAN IP address."""
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.1.194", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "localhost"
