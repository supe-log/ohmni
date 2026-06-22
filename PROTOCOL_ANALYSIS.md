# OhmniLabs Web App WebRTC Control Protocol - Complete Analysis

Reverse-engineered from:
- `main.26e8ce2a.chunk.js` (1.2MB) - application logic
- `2.f8ee70bc.chunk.js` (3.4MB) - vendor/library bundle

---

## 1. Environment Configuration (Hardcoded)

All values are baked in at build time from `process.env.REACT_APP_*`:

```js
{
  ENV: "production",
  API_END_POINT: "https://api.ohmnilabs.com",
  ENTERPRISE_API_END_POINT: "https://enterprise-api.ohmnilabs.com",
  SOCKET_END_POINT: "https://api.ohmnilabs.com",
  ENTERPRISE_END_POINT: "https://app.ohmnilabs.com",
  API_LOGGING_SYSTEM: "https://l79rcoxoh7.execute-api.us-west-2.amazonaws.com/production/write-logs",
  LOG_PLATFORM: "web-client",
  GOOGLE_AUTH_CLIENT_ID: "555570964614-guvmpb7vv37joj4ar64o0j79m9mdio6b.apps.googleusercontent.com",
  SENTRY_DSN: "https://c8b6ca5c0ed64f208516cf2fd2275f3a@sentry.io/1399138",
  ADMIN_END_POINT: "https://admin.ohmnilabs.com",
  ADMIN_API_END_POINT: "https://admin-api.ohmnilabs.com"
}
```

No hardcoded API keys or secret credentials were found. All authentication is token-based via Google OAuth or username/password login.

---

## 2. Google OAuth Flow

**Client ID:** `555570964614-guvmpb7vv37joj4ar64o0j79m9mdio6b.apps.googleusercontent.com`

**Flow:** `auth-code` (server-side exchange)

```js
// Google login button uses @react-oauth/google with auth-code flow
const login = useGoogleLogin({
  onSuccess: (response) => {
    onSuccess({ credential: response.code });  // Google authorization code
  },
  flow: "auth-code",
  onError: onError
});

// Wrapped in GoogleOAuthProvider
<GoogleOAuthProvider clientId={config.GOOGLE_AUTH_CLIENT_ID}>
  <GoogleLoginButton />
</GoogleOAuthProvider>
```

**Server-side exchange:**
```js
// POST https://api.ohmnilabs.com/auth/google
// Body: { credential: "<google_auth_code>" }
// Response: { success: true, data: { token: "<jwt>", username: "<email>" } }
```

---

## 3. Authentication & Login

### Username/Password Login
```js
// POST https://api.ohmnilabs.com/sign_in
// Body: { username: "...", password: "..." }
// Response: { success: true, data: { token: "<jwt>", username: "<email>" } }
```

### Token Storage
```js
// On successful login:
localStorage.setItem("token", data.token);
localStorage.setItem("username", data.username);

// Session tokens (for enterprise impersonation):
sessionStorage.token = token;
sessionStorage.setItem("expirationTime", expirationTime);
```

### Token Retrieval (used everywhere)
```js
this._authToken = localStorage.token || sessionStorage.token;
```

### Get Current User
```js
// GET https://api.ohmnilabs.com/app/user
// Headers: { Authorization: "Bearer <token>" }  (standard JWT auth)
```

---

## 4. API Routes

```js
const apiPaths = {
  signIn: "/sign_in",
  signUp: "/sign_up",
  authGoogle: "/auth/google",
  iceServerUrls: "/api/iceserverurls",
  listBot: "/app/list",
  fetchBotInfo: (botId) => `/app/bots/${botId}/info`,
  enterpriseFetchBotInfo: (botId) => `/enterprise/call_actions/bot_calling/${botId}`,
  enterpriseFetchIceServers: "/enterprise/call_actions/ice_servers",
  getShareLinkList: (botId) => `/app/bots/${botId}/share_link/list`,
  createShareLink: (botId) => `/app/bots/${botId}/share_link/create`,
  removeShareLink: (botId) => `/app/bots/${botId}/share_link/remove`,
  requestShareLink: (botId) => `/app/bots/${botId}/share_link/request`,
  user: "/app/user",
  updateUser: "/app/user/update",
  getUserCallingInfo: "/enterprise/call_actions/user_calling",
  remoteReboot: (botId) => `/app/bots/${botId}/trigger_reboot`,
  reportInCallIssue: "/app/in_call/report_issue"
};
```

**Hosts:**
- Personal bots: `https://api.ohmnilabs.com`
- Enterprise bots: `https://enterprise-api.ohmnilabs.com`

---

## 5. Socket.IO Connection Setup

```js
// Connection
const socket = io(SOCKET_END_POINT, {
  path: "/socket.io.web"
});
// SOCKET_END_POINT = "https://api.ohmnilabs.com"
// So full URL: wss://api.ohmnilabs.com/socket.io.web

// On connect, immediately authenticate:
socket.on("connect", () => {
  socket.emit("auth", {
    token: localStorage.token || sessionStorage.token,
    callId: callId  // UUID generated for this call session
  });
});
```

### Socket Events Listened To:

| Event | Description |
|-------|-------------|
| `connect` | Socket connected, triggers auth |
| `disconnect` | Socket disconnected |
| `rtc` | WebRTC signaling messages |
| `streamlist` | List of available streams/bots |
| `battery` | Battery status updates |
| `busy` | Bot is busy/in-call |
| `auth-resolved` | Auth result with ICE credentials |

### `auth-resolved` Response:
```js
socket.on("auth-resolved", (data) => {
  // data = { success: true, iceServerCredential: { username: "...", credential: "..." } }
  if (data.success) {
    this.iceServerCredential = data.iceServerCredential;
    // Merge ICE credentials into ICE server config
    if (this._config.peerConnectionConfig.iceServers) {
      this.setIceServers(this._config.peerConnectionConfig.iceServers);
    }
  }
  if (this._authCallback) this._authCallback(data);
});
```

---

## 6. WebRTC Peer Connection Setup

### PeerConnection Configuration
```js
const config = {
  peerConnectionConfig: {
    isAutonomy: false,  // or true for autonomy mode
    enableMedia: true
    // iceServers: [...] // populated after auth-resolved
  },
  peerConnectionConstraints: {
    optional: [{ DtlsSrtpKeyAgreement: true }]
  }
};
```

### ICE Server Setup
```js
setIceServers(servers) {
  const iceServers = servers.map(server => {
    let s = server;
    // Normalize url -> urls
    if (!s.urls && s.url) {
      s = JSON.parse(JSON.stringify(s));
      s.urls = s.url;
    }
    // Merge in TURN credentials from auth-resolved
    return { ...s, ...this.iceServerCredential };
  });
  this._config.peerConnectionConfig.iceServers = iceServers;
}
```

### Peer Creation
```js
addPeer(remoteId, payload) {
  const peer = new Peer(
    this,           // manager reference
    remoteId,       // bot ID
    this._config.peerConnectionConfig,
    this._config.peerConnectionConstraints,
    payload         // optional: { snapshot_disabled: bool, bot: botObj }
  );
  this._peerDatabase[remoteId] = peer;
  return peer;
}

// Inside Peer constructor:
this.pc = new RTCPeerConnection(peerConnectionConfig, {
  ...peerConnectionConstraints,
  optional: [{ DtlsSrtpKeyAgreement: true }]
});
```

---

## 7. WebRTC Signaling via `rtc` Event

### Sending RTC Messages
```js
sendRtc(type, to, payload) {
  this._socket.emit("rtc", {
    to: to,          // target bot ID
    type: type,      // "init" | "offer" | "answer" | "candidate" | "stop"
    payload: payload,
    extra: {
      call_session_id: this.callId
    }
  });
}
```

### Receiving RTC Messages
```js
socket.on("rtc", (message) => {
  // message = { from: "<botId>", type: "<type>", payload: <data> }
  handleRtc(message);
});
```

### handleRtc - Full Implementation
```js
handleRtc(message) {
  const remoteId = message.from;

  if (message.type === "extra") {
    this.emit("extra", message.payload);
    return;
  }

  const peer = this._peerDatabase[remoteId] || this.addPeer(remoteId);
  const pc = peer.pc;

  switch (message.type) {
    case "init":
      this.offer(remoteId);
      break;

    case "offer":
      pc.setRemoteDescription(new RTCSessionDescription(message.payload))
        .then(() => {
          this.answer(remoteId);
          peer.addIceCandidatesFromQueue();
        });
      break;

    case "answer":
      pc.setRemoteDescription(new RTCSessionDescription(message.payload))
        .then(() => {
          peer.addIceCandidatesFromQueue();
        });
      break;

    case "candidate":
      const candidate = new RTCIceCandidate({
        sdpMLineIndex: message.payload.label,
        sdpMid: message.payload.id,
        candidate: message.payload.candidate
      });
      if (pc.remoteDescription) {
        pc.addIceCandidate(candidate);
      } else {
        peer.saveIceCandidateToQueue(candidate);
      }
      break;

    case "stop":
      this.emit("remote_hangup");
      break;

    case "bot_in_use":
      this.emit("bot_in_use");
      break;
  }
}
```

### Offer / Answer
```js
offer(remoteId, options = {}) {
  const pc = this._peerDatabase[remoteId].pc;
  pc.createOffer(options)
    .then(desc => pc.setLocalDescription(desc))
    .then(() => {
      this.sendRtc("offer", remoteId, pc.localDescription);
    });
}

answer(remoteId) {
  const pc = this._peerDatabase[remoteId].pc;
  pc.createAnswer()
    .then(desc => pc.setLocalDescription(desc))
    .then(() => {
      this.sendRtc("answer", remoteId, pc.localDescription);
    });
}
```

### ICE Candidate Format (sent)
```js
// onicecandidate:
this.manager.sendRtc("candidate", remoteId, {
  label: candidate.sdpMLineIndex,
  id: candidate.sdpMid,
  candidate: candidate.candidate
});
```

---

## 8. Data Channel Setup

### Primary Data Channel (commands & control)
```js
setupDataChannel() {
  const channel = this.pc.createDataChannel(this.remoteId);  // label = botId
  this.channel = channel;
  this.channel.binaryType = "arraybuffer";

  this.channel.onopen = () => {
    console.log("Peer channel: onopen datachannel:", this.remoteId);
  };

  this.channel.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      this.manager.handleChannelJson(this, data);  // emits "dc_json"
    } catch (e) {
      console.log("Failed to parse data channel message:", e);
    }
  };
}
```

### Bulk Data Channel (file transfers / snapshots)
```js
setupBulkDataChannel() {
  const bulkChannel = this.pc.createDataChannel("bulk", { ordered: true });
  this.bulkChannel = bulkChannel;
  this.bulkChannel.binaryType = "arraybuffer";

  // Binary protocol: first 12 bytes are header
  this.bulkChannel.onmessage = (event) => {
    const view = new DataView(event.data);
    const rtype = view.getInt32(0);   // record type
    const rval = view.getInt32(4);    // value/size
    const rextra = view.getInt32(8);  // extra data
    // ... handles file transfer chunks
  };
}
```

### getActiveDataChannel
```js
getActiveDataChannel() {
  return (this.channel === null || this.channel.readyState !== "open")
    ? null
    : this.channel;
}
```

---

## 9. TeleProto - Command Protocol Parser

This is the protocol that packages all commands for the data channel:

```js
class TeleProto {
  constructor() {
    this.data = null;
  }

  // Static method - creates the wire format
  static send(type, code, data) {
    return { type: type, code: code, data: data };
    // type: "mgs" | "tuple" | "cmd"
    // code: numeric code or null
    // data: value, [x,y] array, or JSON object
  }

  // Build a single-value message
  buildAndSend(code, value) {
    this.data = TeleProto.send("mgs", code, value);
  }

  // Build a tuple (2-value) message
  sendTuple(code, x, y) {
    this.data = TeleProto.send("tuple", code, [x, y]);
  }

  // Build a JSON command message
  sendJsonToBot(jsonObj) {
    this.data = TeleProto.send("cmd", null, jsonObj);
  }

  // Parse a string command like "MOTION:0.5,0.3" into wire format
  parse(input) {
    this.data = null;
    const colonIdx = input.indexOf(":");
    if (colonIdx === -1) return this.data;

    const [command, value] = [input.substr(0, colonIdx), input.substr(colonIdx + 1)];

    switch (command) {
      case "JSONCMD":
        this.sendJsonToBot({ type: "jsoncmd", jsonstr: value });
        break;
      case "LEFT_STICK_X":
        this.buildAndSend(Codes.LEFT_STICK_X, parseFloat(value));
        break;
      case "LEFT_STICK_Y":
        this.buildAndSend(Codes.LEFT_STICK_Y, parseFloat(value));
        break;
      case "RIGHT_STICK_X":
        this.buildAndSend(Codes.RIGHT_STICK_X, parseFloat(value));
        break;
      case "RIGHT_STICK_Y":
        this.buildAndSend(Codes.RIGHT_STICK_Y, parseFloat(value));
        break;
      case "FACE_1":
        this.buildAndSend(Codes.FACE_1, 1);
        break;
      case "HEAD":
        const [hx, hy] = value.split(",");
        this.buildAndSend(Codes.LOOK_X, parseFloat(hx));
        this.buildAndSend(Codes.LOOK_Y, parseFloat(hy));
        break;
      case "HEAD_DELTA":
        const [hdx, hdy] = value.split(",");
        this.sendTuple(Codes.LOOK_DELTA, parseFloat(hdx), parseFloat(hdy));
        break;
      case "MOTION":
        const [my, mx] = value.split(",");
        this.sendTuple(Codes.MOTION, parseFloat(my), parseFloat(mx));
        break;
      case "MM_DELTA":
        const [mdx, mdy] = value.split(",");
        this.sendTuple(Codes.MM_DELTA, parseFloat(mdx), parseFloat(mdy));
        break;
      case "NECKVEL":
        const [nvx, nvy] = value.split(",");
        this.sendTuple(Codes.NECKVEL, parseFloat(nvx), parseFloat(nvy));
        break;
      case "CENTER_LOCK":
        this.buildAndSend(Codes.CENTER_LOCK, parseFloat(value));
        break;
      case "MWHEEL":
        this.buildAndSend(Codes.ZOOM_DELTA, parseFloat(value) / 10);
        break;
      case "disconnect":
        this.buildAndSend(Codes.DISCONNECT, 1);
        break;
    }
    return this.data;
  }
}

// Numeric codes for each command type
TeleProto.Codes = {
  CONNECT:       0,
  DISCONNECT:    1,
  LEFT_STICK_X:  8,
  LEFT_STICK_Y:  9,
  RIGHT_STICK_X: 10,
  RIGHT_STICK_Y: 11,
  FACE_1:        20,
  LOOK_X:        60,
  LOOK_Y:        61,
  LOOK_DELTA:    62,
  CENTER_LOCK:   63,
  MM_DELTA:      64,
  NECKVEL:       65,
  MOTION:        70,
  ZOOM_DELTA:    80
};
```

---

## 10. sendJsonCmd - How JSON Commands Are Sent

```js
sendJsonCmd(cmdObj) {
  const proto = new TeleProto();
  if (this.callConfiguration.botId != null) {
    this.emit("ON_SEND_COMMAND", cmdObj);

    // Step 1: Wrap in "JSONCMD:" prefix
    let msg = "JSONCMD:" + JSON.stringify(cmdObj);

    // Step 2: Parse through TeleProto (creates { type: "cmd", code: null, data: { type: "jsoncmd", jsonstr: "<json>" } })
    msg = JSON.stringify(proto.parse(msg));

    // Step 3: Send over data channel
    this.peerManager.sendData(this.callConfiguration.botId, msg);
  }
}
```

### Wire format for a JSON command:
```json
{
  "type": "cmd",
  "code": null,
  "data": {
    "type": "jsoncmd",
    "jsonstr": "{\"cmd\":\"move\",\"lspeed\":-250,\"rspeed\":250,\"time\":2000}"
  }
}
```

### sendData (PeerManager)
```js
sendData(peerId, data) {
  const peer = this._peerDatabase[peerId];
  if (peer) {
    const channel = peer.getActiveDataChannel();
    if (channel !== null) {
      channel.send(data);  // data is a JSON string
    }
  }
}
```

---

## 11. Movement Commands

### Keyboard-Driven Movement (WASD / Arrow Keys)

The InputManager translates keyboard input into MOTION tuples:

```js
// Key codes:
// W/ArrowUp (87/38) = forward
// S/ArrowDown (83/40) = backward
// A/ArrowLeft (65/37) = turn left
// D/ArrowRight (68/39) = turn right
// R (82) = neck tilt up
// F (70) = neck tilt down

resolveKeyMotion() {
  const driveSpeed = this.senval.keysDriveSpeed;  // default: 1.0
  const rotateSpeed = this.senval.keysRotateSpeed * this.senval.ratioRotateSpeed;
  // keysRotateSpeed default: 1.0, ratioRotateSpeed default: 0.4
  const motion = this.motion_last;
  const keys = this.downmap;

  let forward = 0;
  if (keys[87] === 1 || keys[38] === 1) forward += driveSpeed;   // W or Up
  if (keys[83] === 1 || keys[40] === 1) forward -= driveSpeed;   // S or Down

  let rotation = 0;
  if (keys[68] === 1 || keys[39] === 1) rotation += rotateSpeed; // D or Right
  if (keys[65] === 1 || keys[37] === 1) rotation -= rotateSpeed; // A or Left
  rotation *= forward ? Math.sign(forward) : 1;

  // Neck tilt
  let neckTilt = 0;
  const neckSpeed = this.senval.keysNeckTiltSpeed;  // default: 1
  if (keys[82] === 1) neckTilt = 12 * neckSpeed;    // R = tilt up
  if (keys[70] === 1) neckTilt = -12 * neckSpeed;   // F = tilt down

  this.updateMstate(rotation, forward);
  // -> emits: "input", "MOTION:<forward>,<rotation>"

  if (neckTilt !== this.neck_motion_last.pitchvel) {
    this.neck_motion_last.pitchvel = neckTilt;
    this.actionTuple("NECKVEL", 0, neckTilt);
    // -> emits: "input", "NECKVEL:0,<pitchvel>"
  }
}

// updateMstate sends:
actionTuple("MOTION", forward.toFixed(4), rotation.toFixed(4));
// -> emit("input", "MOTION:<forward>,<rotation>")
```

### Wire Format for Movement
```js
// MOTION command (continuous driving)
// emit("input", "MOTION:0.5000,0.3000")
// -> TeleProto.parse("MOTION:0.5000,0.3000")
// -> { type: "tuple", code: 70, data: [0.5, 0.3] }
// Sent as: '{"type":"tuple","code":70,"data":[0.5,0.3]}'

// NECKVEL command (neck tilt velocity)
// -> { type: "tuple", code: 65, data: [0, 12] }
// Sent as: '{"type":"tuple","code":65,"data":[0,12]}'
```

### Speed Defaults
```js
MAX_SPEED = { LINEAR: 2, ROTATION: 1.2 };
DEFAULT_SPEED = { LINEAR: 1, ROTATION: 1, ROTATION_RATIO: 0.4, GEN12_ROTATION_RATIO: 0.5 };
```

### JSON-Based Move Command (used for leaveDock)
```js
sendJsonCmd({ cmd: "move", lspeed: -250, rspeed: 250, time: 2000 });
// Wire: {"type":"cmd","code":null,"data":{"type":"jsoncmd","jsonstr":"{\"cmd\":\"move\",\"lspeed\":-250,\"rspeed\":250,\"time\":2000}"}}
```

---

## 12. Neck Position (setNeckPosition)

```js
// Direct position command:
sendJsonCmd({ cmd: "setNeckPosition", pos: 550, ival: 100 });
// pos: servo position (250-550 observed range)
// ival: interval/speed in ms

// Nodding sequence:
sendJsonCmd({ cmd: "setNeckPosition", pos: 550, ival: 100 });  // look up
await delay(800);
sendJsonCmd({ cmd: "setNeckPosition", pos: 250, ival: 100 });  // look down
await delay(800);
sendJsonCmd({ cmd: "setNeckPosition", pos: 550, ival: 100 });  // back to center

// Continuous neck velocity (from keyboard R/F):
// -> { type: "tuple", code: 65, data: [0, 12] }   // tilt up at speed 12
// -> { type: "tuple", code: 65, data: [0, -12] }  // tilt down at speed -12
// -> { type: "tuple", code: 65, data: [0, 0] }    // stop tilting
```

---

## 13. All Known JSON Commands (via sendJsonCmd)

| Command | Payload | Description |
|---------|---------|-------------|
| `move` | `{ cmd: "move", lspeed: <int>, rspeed: <int>, time: <ms> }` | Direct wheel control (left/right speed in mm/s, duration) |
| `setNeckPosition` | `{ cmd: "setNeckPosition", pos: <int>, ival: <int> }` | Set neck servo position |
| `autodock` | `{ cmd: "autodock", visionBasedCalibrationEnabled: <bool> }` | Auto-dock to charger |
| `autodockCalibrate` | `{ cmd: "autodockCalibrate", visionBasedCalibrationEnabled: <bool> }` | Calibrate auto-dock |
| `setSpeakerVolume` | `{ cmd: "setSpeakerVolume", value: <int> }` | Set bot speaker volume |
| `setBrightness` | `{ cmd: "setBrightness", value: <int> }` | Set screen brightness |
| `setMicLevel` | `{ cmd: "setMicLevel", value: <int> }` | Set microphone gain |
| `setMicEnabled` | `{ cmd: "setMicEnabled", value: <0\|1> }` | Enable/disable mic |
| `setManualExposure` | `{ cmd: "setManualExposure", v: <int>, cam?: "aux" }` | Set camera exposure |
| `resetAutoexposure` | `{ cmd: "resetAutoexposure", cam?: "aux" }` | Reset to auto-exposure |
| `setManualContrast` | `{ cmd: "setManualContrast", v: <int> }` | Set camera contrast |
| `resetContrast` | `{ cmd: "resetContrast" }` | Reset contrast |
| `setManualSharpness` | `{ cmd: "setManualSharpness", v: <int> }` | Set camera sharpness |
| `resetSharpness` | `{ cmd: "resetSharpness" }` | Reset sharpness |
| `setLightColor` | `{ cmd: "setLightColor", h: <0-255>, s: <0-255>, v: <0-255> }` | Set LED color (HSV) |
| `videoEnable` | `{ cmd: "videoEnable", cmdstring: { isVideoEnabled: <bool> } }` | Toggle video feed |
| `snapshot` | `{ cmd: "snapshot", data: { width?: 1280, height?: 720 } }` | Take photo |
| `startScreenSharing` | `{ cmd: "startScreenSharing" }` | Notify bot of screen share start |
| `stopScreenSharing` | `{ cmd: "stopScreenSharing" }` | Notify bot of screen share stop |
| `clickLook` | `{ cmd: "clickLook", data: <coords> }` | Click-to-look |
| `collisionStop` | `{ cmd: "collisionStop", v: <bool> }` | Toggle collision avoidance |
| `setIsInDebugMode` | `{ cmd: "setIsInDebugMode", isInDebugMode: <bool> }` | Toggle debug mode |
| `sendBotshellCmd` | `{ cmd: "sendBotshellCmd", commandName: "<cmd>", parameterArray: [] }` | Generic shell command |
| `auxCamCalibrate` | `{ cmd: "auxCamCalibrate" }` | Calibrate auxiliary camera |
| `updateAuxCamCalibrate` | `{ cmd: "updateAuxCamCalibrate", downCamCPoint: <data> }` | Update aux cam calibration |
| `say` | Via terminal: `{ cmd: "say", ... }` | Text-to-speech |

### Botshell Commands (via sendBotshellCmd)
```js
sendJsonCmd({ cmd: "sendBotshellCmd", commandName: "leave_dock", parameterArray: [] });
sendJsonCmd({ cmd: "sendBotshellCmd", commandName: "stop_leave_dock", parameterArray: [] });
```

---

## 14. Data Channel Incoming Messages (from bot)

The bot sends JSON over the data channel. Parsed types:

| type | Fields | Description |
|------|--------|-------------|
| `odo` | `{ l, r }` | Odometry (left/right wheel) |
| `battery` | `{ v, c, d, mas, p, fet, stat }` | Battery voltage, current, docked, coulomb, percentage |
| `cpu_info` | `{ data: { freq: [], temp: [] } }` | CPU frequencies and temperatures |
| `collisionDetectionRanges` | `{ data }` | Collision sensor ranges |
| `collisionKeyViz` | `{ data }` | Collision key visualization |
| `set_language_speech_recognize` | `{ language }` | Robot language setting |
| `ArdockBegin` | - | Auto-dock started |
| `ArdockCanceled` | - | Auto-dock cancelled |
| `ArdockMissingCalibration` | - | Calibration needed |
| `ArdockSearching` | - | Searching for dock |
| `ArdockDocking` | - | Actively docking |
| `ArdockDone` | - | Docking complete |
| `ArdockFailed` | - | Docking failed |

---

## 15. Full Call Flow (End-to-End)

### Step 1: Authentication
```
User logs in via Google OAuth or username/password
  -> POST /auth/google or POST /sign_in
  -> Receives JWT token
  -> Stored in localStorage.token
```

### Step 2: List Bots
```
GET /app/list
  -> Returns array of bot objects with _id, name, status, etc.
```

### Step 3: Initiate Call (Click "Call" button)
```js
// setCallParams dispatched to Redux store:
setCallParams({ botId: bot._id, enterpriseId: bot.enterpriseId, sharelinkToken: token });

// Navigate to call page
history.push("/call" or "/new-call");
```

### Step 4: Fetch Bot Info
```
GET /app/bots/{botId}/info
  -> Returns bot details: vername, camera_info, fk_camera_enabled, lcolor, etc.
```

### Step 5: CallManager.startCall(bot)
```js
startCall(bot) {
  const botId = bot._id;
  const vername = bot.vername;

  // Configure call settings from bot info
  this.callConfiguration.cameraInfo = bot.camera_info;
  this.connectionStatus = "connecting";

  // Step 5a: Get user media (camera + mic)
  this.mediaManager.start().then(stream => {
    this._localStream = stream;
    this.peerManager._localStream = stream;

    // Step 5b: Add local stream to peer
    this.peerManager.addLocalStream(botId, stream, {
      snapshot_disabled: this.snapshotDisabled,
      bot: bot
    });

    // Step 5c: Initialize peer connection
    this.peerManager.peerInit(botId, {
      snapshot_disabled: this.snapshotDisabled,
      bot: bot
    });

    // Step 5d: Start connection check timer
    this.checkConnection(botId);
  });
}
```

### Step 6: PeerManager.peerInit(botId)
```js
async peerInit(botId, payload) {
  // Wait for ICE credentials (up to ~20 seconds)
  let retries = 0;
  while (retries < 100) {
    if (this.iceServerCredential) break;
    await sleep(200);
    retries++;
  }

  // Create peer if not exists
  const peer = this._peerDatabase[botId] || this.addPeer(botId, payload);

  // Send "init" to bot via signaling server
  this.sendRtc("init", botId, this._config.peerConnectionConfig);
}
```

### Step 7: WebRTC Handshake
```
Client                    Server                    Bot
  |--- rtc:init ----------->|--- rtc:init ----------->|
  |                         |                         |
  |<-- rtc:offer -----------|<-- rtc:offer -----------|
  |--- setRemoteDesc ------>|                         |
  |--- createAnswer ------->|                         |
  |--- rtc:answer --------->|--- rtc:answer --------->|
  |                         |                         |
  |<-- rtc:candidate -------|<-- rtc:candidate -------|
  |--- addIceCandidate ---->|                         |
  |--- rtc:candidate ------>|--- rtc:candidate ------>|
  |                         |                         |
  |<====== Data Channel + Media Stream =============>|
```

Note: The client also sets up two data channels proactively during Peer construction:
1. `createDataChannel(botId)` - primary command channel
2. `createDataChannel("bulk", { ordered: true })` - file/snapshot transfers

### Step 8: Connection Established
```js
// Data channel opens -> can now send commands
// Remote media stream received -> video displayed

// InputManager is enabled -> keyboard/mouse events start flowing
// Periodic tick sends MOTION tuples at regular intervals while keys held
```

### Step 9: Sending Movement Commands
```js
// Keyboard press W:
// -> resolveKeyMotion() calculates forward=1.0, rotation=0
// -> actionTuple("MOTION", "1.0000", "0.0000")
// -> emit("input", "MOTION:1.0000,0.0000")
// -> TeleProto.parse("MOTION:1.0000,0.0000")
// -> { type: "tuple", code: 70, data: [1.0, 0.0] }
// -> JSON.stringify -> '{"type":"tuple","code":70,"data":[1,0]}'
// -> dataChannel.send(jsonString)
```

### Step 10: Hangup
```js
hangup() {
  this.peerManager.stopAll();  // sends "stop" rtc message, closes peer connections
  this.mediaManager.stop();    // stops local media streams
}

// peerManager.stop(botId):
stop(botId, isRestart) {
  const peer = this._peerDatabase[botId];
  peer.closeConnections();
  const type = isRestart ? "restart" : "stop";
  this.sendRtc(type, botId, {});  // notify bot via signaling
  delete this._peerDatabase[botId];
}
```

---

## 16. Share Link / Temporary Access Flow

### Creating a Share Link (bot owner)
```
POST /app/bots/{botId}/share_link/create
  -> Returns share link token
```

### Using a Share Link (guest)
```js
// URL: https://app.ohmnilabs.com/share/<token>

// Step 1: Fetch bot info from share token
CallManager.initSharingToken(token) {
  const data = { jwtToken: localStorage.token || sessionStorage.token };
  // POST https://enterprise-api.ohmnilabs.com/enterprise/call_actions/sharelink/<token>
  // Body: { jwtToken: "<jwt>" }
  // Response: { data: { botId, enterpriseId, ... } }
}

// Step 2: Normal call flow with sharelinkToken passed through
setCallParams({ botId, enterpriseId, sharelinkToken: token });
```

---

## 17. Constants Reference

```js
// Camera resolutions
CAMERA_RESOLUTION = {
  NORMAL_CAM: { width: 1280, height: 1024 },
  USB_VP_CU135: { width: 1280, height: 960 },
  USB_VP_ECAM82: { width: 1280, height: 720 }
};

// Camera types (USB vendor:product IDs)
CAMERA_TYPES = {
  USB_VP_CU135: "2560-c1d1",
  USB_VP_ECAM82: "2560-c181",
  USB_VP_HBV4K: "1bcf-c001"
};

// Visual settings ranges
ABSTRACT_VALUE = { MIN: 0, MAX: 100, DEFAULT: 50 };
VISUAL_CONTRAST = { MIN: 0, MAX: 30, DEFAULT: 15 };
VISUAL_SHARPNESS = { MIN: 0, MAX: 127, DEFAULT: 16 };

// Connection states
ConnectionStatus = {
  FAILED: "failed",
  DISCONNECTED: "disconnected",
  CONNECTING: "connecting",
  MEDIA_ERROR: "media_error",
  TIMEOUT: "timeout",
  OPEN: "open",
  UNKNOWN: "unknown"
};
```

---

## 18. Node.js Client Implementation Notes

To build a standalone client, you need:

1. **socket.io-client** - Connect to `https://api.ohmnilabs.com` with path `/socket.io.web`
2. **wrtc** (node-webrtc) or similar - For RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
3. **Authentication** - Either:
   - POST to `/sign_in` with username/password to get JWT token
   - Or use an existing token from a browser session

**Minimal flow:**
```js
const io = require("socket.io-client");
const { RTCPeerConnection, RTCSessionDescription, RTCIceCandidate } = require("wrtc");

const TOKEN = "your-jwt-token";
const BOT_ID = "your-bot-id";
const CALL_ID = generateUUID();

// 1. Connect socket
const socket = io("https://api.ohmnilabs.com", { path: "/socket.io.web" });

// 2. Authenticate on connect
socket.on("connect", () => {
  socket.emit("auth", { token: TOKEN, callId: CALL_ID });
});

// 3. Wait for auth + ICE credentials
let iceCredential = null;
socket.on("auth-resolved", (data) => {
  if (data.success) {
    iceCredential = data.iceServerCredential;
    initCall();
  }
});

// 4. Create peer connection and data channel
function initCall() {
  const pc = new RTCPeerConnection({
    // ICE servers come from auth-resolved, merged with iceCredential
  });

  const dataChannel = pc.createDataChannel(BOT_ID);
  dataChannel.binaryType = "arraybuffer";

  dataChannel.onopen = () => {
    console.log("Connected! Can send commands now.");
    // Send a move command:
    sendJsonCmd(dataChannel, { cmd: "setNeckPosition", pos: 400, ival: 100 });
  };

  dataChannel.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    console.log("From bot:", msg);
  };

  // Handle signaling
  pc.onicecandidate = (event) => {
    if (event.candidate) {
      socket.emit("rtc", {
        to: BOT_ID,
        type: "candidate",
        payload: {
          label: event.candidate.sdpMLineIndex,
          id: event.candidate.sdpMid,
          candidate: event.candidate.candidate
        },
        extra: { call_session_id: CALL_ID }
      });
    }
  };

  socket.on("rtc", (msg) => handleRtc(pc, msg));

  // Initiate the call
  socket.emit("rtc", {
    to: BOT_ID,
    type: "init",
    payload: {},
    extra: { call_session_id: CALL_ID }
  });
}

// 5. Command helpers
function sendJsonCmd(channel, cmdObj) {
  const inner = JSON.stringify(cmdObj);
  const payload = JSON.stringify({
    type: "cmd",
    code: null,
    data: { type: "jsoncmd", jsonstr: inner }
  });
  channel.send(payload);
}

function sendMotion(channel, forward, rotation) {
  const payload = JSON.stringify({
    type: "tuple",
    code: 70,  // MOTION
    data: [forward, rotation]
  });
  channel.send(payload);
}

function sendNeckVelocity(channel, pitchVel) {
  const payload = JSON.stringify({
    type: "tuple",
    code: 65,  // NECKVEL
    data: [0, pitchVel]
  });
  channel.send(payload);
}
```

**Important: Motion commands must be sent repeatedly** (the web app sends them on a tick interval while keys are held down). Sending a single MOTION tuple will only move briefly. Send `MOTION:[0,0]` to stop.
