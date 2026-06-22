const http = require('http');
const fs = require('fs');
const path = require('path');
const net = require('net');
const { spawn } = require('child_process');

const { OhmniMapper } = require('./mapper');
const { LidarReader } = require('./lidar_reader');

const PORT = 3333;
const BOT_SHELL_PORT = 9999; // ADB-forwarded bot_shell.sock
const ROBOT_IP = '192.168.1.194';
const ROBOT_ADB_ADDR = `${ROBOT_IP}:5555`;

// --- ADB Connection Persistence ---
let adbConnected = false;
let adbCheckInterval = null;

async function checkAdbConnection() {
  return new Promise((resolve) => {
    const p = spawn('adb', ['devices']);
    let out = '';
    p.stdout.on('data', (d) => { out += d.toString(); });
    p.on('close', () => {
      resolve(out.includes(ROBOT_ADB_ADDR) && out.includes('device'));
    });
    setTimeout(() => { try { p.kill(); } catch {} resolve(false); }, 3000);
  });
}

async function connectAdb() {
  return new Promise((resolve) => {
    const p = spawn('adb', ['connect', ROBOT_ADB_ADDR]);
    let out = '';
    p.stdout.on('data', (d) => { out += d.toString(); });
    p.on('close', () => {
      const ok = out.includes('connected') && !out.includes('unable');
      resolve(ok);
    });
    setTimeout(() => { try { p.kill(); } catch {} resolve(false); }, 5000);
  });
}

async function setupAdbForward() {
  return new Promise((resolve) => {
    const p = spawn('adb', ['-s', ROBOT_ADB_ADDR, 'forward',
      'tcp:' + BOT_SHELL_PORT,
      'localfilesystem:/data/data/com.ohmnilabs.telebot_rtc/files/bot_shell.sock']);
    p.on('close', (code) => {
      if (code === 0) console.log('ADB port forward established for bot_shell');
      resolve(code === 0);
    });
    setTimeout(() => { try { p.kill(); } catch {} resolve(false); }, 3000);
  });
}

async function ensureAdbConnection() {
  const connected = await checkAdbConnection();
  if (connected) {
    if (!adbConnected) {
      console.log('ADB connected to', ROBOT_ADB_ADDR);
      adbConnected = true;
      // Root, forward, and re-detect cameras on reconnect
      await new Promise((resolve) => {
        const p = spawn('adb', ['-s', ROBOT_ADB_ADDR, 'root']);
        p.on('close', () => setTimeout(resolve, 1000));
        setTimeout(() => { try { p.kill(); } catch {} resolve(); }, 3000);
      });
      await setupAdbForward();
      detectCameras().catch(() => {});
      // Reconnect bot_shell after forward is re-established
      if (!botSocket || botSocket.destroyed) connectToBot();
    }
    return true;
  }
  adbConnected = false;
  console.log('ADB not connected, attempting reconnect to', ROBOT_ADB_ADDR, '...');
  const ok = await connectAdb();
  if (ok) {
    adbConnected = true;
    console.log('ADB reconnected to', ROBOT_ADB_ADDR);
    await new Promise((resolve) => {
      const p = spawn('adb', ['-s', ROBOT_ADB_ADDR, 'root']);
      p.on('close', () => setTimeout(resolve, 1000));
      setTimeout(() => { try { p.kill(); } catch {} resolve(); }, 3000);
    });
    await setupAdbForward();
    detectCameras().catch(() => {});
    if (!botSocket || botSocket.destroyed) connectToBot();
  } else {
    console.log('ADB reconnect failed');
  }
  return ok;
}

// Check ADB connection every 10 seconds
adbCheckInterval = setInterval(ensureAdbConnection, 10000);
ensureAdbConnection();

// --- USB Device and LIDAR Diagnostics ---
async function getUsbDevices() {
  return new Promise((resolve) => {
    const adb = spawn('adb', ['-s', ROBOT_ADB_ADDR, 'shell',
      'ls -la /dev/ttyUSB* /dev/ttyACM* 2>/dev/null; echo "---LSUSB---"; cat /sys/kernel/debug/usb/devices 2>/dev/null || ls /sys/bus/usb/devices/ 2>/dev/null; echo "---VIDEO---"; ls -la /dev/video* 2>/dev/null']);
    let out = '';
    adb.stdout.on('data', (d) => { out += d.toString(); });
    adb.stderr.on('data', () => {});
    adb.on('close', () => resolve(out));
    setTimeout(() => { try { adb.kill(); } catch {} resolve('timeout'); }, 5000);
  });
}

async function getLidarStatus() {
  return new Promise((resolve) => {
    // Check for LIDAR-related serial devices and processes
    const adb = spawn('adb', ['-s', ROBOT_ADB_ADDR, 'shell',
      'echo "=== SERIAL DEVICES ==="; ls -la /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/* /dev/serial/by-path/* 2>/dev/null || echo "no serial devices"; ' +
      'echo "=== USB DEVICES ==="; lsusb 2>/dev/null || cat /sys/kernel/debug/usb/devices 2>/dev/null | grep -A5 "Product" | head -60 || ls /sys/bus/usb/devices/ 2>/dev/null; ' +
      'echo "=== LIDAR PROCESSES ==="; ps -A 2>/dev/null | grep -i "lidar\\|rplidar\\|sllidar\\|scan" | grep -v grep || echo "no lidar processes"; ' +
      'echo "=== USB PORT INFO ==="; for d in /sys/bus/usb/devices/*/product; do echo "$d: $(cat $d 2>/dev/null)"; done 2>/dev/null; ' +
      'echo "=== DMESG USB ==="; dmesg 2>/dev/null | grep -i "usb\\|ttyUSB\\|ttyACM\\|serial\\|lidar" | tail -20 || echo "no dmesg access"']);
    let out = '';
    adb.stdout.on('data', (d) => { out += d.toString(); });
    adb.stderr.on('data', () => {});
    adb.on('close', () => resolve(out));
    setTimeout(() => { try { adb.kill(); } catch {} resolve('timeout'); }, 8000);
  });
}

// Persistent connection to bot_shell
let botSocket = null;
let botBuffer = '';
let responseCallback = null;
let botReady = false;
let lastMoveTime = 0;

function connectToBot() {
  if (botSocket && !botSocket.destroyed) return;
  botReady = false;
  botSocket = new net.Socket();
  botSocket.connect(BOT_SHELL_PORT, '127.0.0.1', () => {
    console.log('Connected to bot_shell');
  });
  botSocket.on('data', (data) => {
    const text = data.toString();
    // Ignore the welcome banner
    if (!botReady) {
      if (text.includes('bot_shell')) {
        botReady = true;
        console.log('bot_shell ready');
        // Wake the head servo so neck_angle commands work
        botSocket.write('wake_head\n');
        return;
      }
    }
    botBuffer += text;
    if (responseCallback) {
      const cb = responseCallback;
      responseCallback = null;
      setTimeout(() => cb(botBuffer), 100);
    }
  });
  botSocket.on('error', (err) => {
    console.error('Bot socket error:', err.message);
    botSocket = null;
    botReady = false;
  });
  botSocket.on('close', () => {
    console.log('Bot socket closed, reconnecting in 3s...');
    botSocket = null;
    botReady = false;
    setTimeout(connectToBot, 3000);
  });
}

function sendCommand(cmd) {
  return new Promise((resolve) => {
    if (!botSocket || botSocket.destroyed || !botReady) {
      connectToBot();
      // Retry once after a delay, don't loop forever
      setTimeout(() => {
        if (!botSocket || botSocket.destroyed || !botReady) {
          resolve('error: bot_shell not connected');
          return;
        }
        sendCommand(cmd).then(resolve);
      }, 1500);
      return;
    }
    botBuffer = '';
    responseCallback = resolve;
    botSocket.write(cmd + '\n');
    setTimeout(() => {
      if (responseCallback) {
        responseCallback = null;
        resolve(botBuffer || 'command sent');
      }
    }, 1000);
  });
}

connectToBot();

// --- Check-in data persistence ---
const CHECKINS_FILE = path.join(__dirname, 'checkins.json');
let checkinNeckPos = 480; // default neck position for check-in (slightly above center)

function loadCheckins() {
  try { return JSON.parse(fs.readFileSync(CHECKINS_FILE, 'utf8')); }
  catch { return []; }
}

function saveCheckins(data) {
  fs.writeFileSync(CHECKINS_FILE, JSON.stringify(data, null, 2));
}

// --- Check-in sessions (link robot screen <-> visitor phone) ---
// States: 'waiting' -> 'submitted' -> 'photo' -> 'done'
const checkinSessions = new Map();

function createSession() {
  const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  checkinSessions.set(id, { id, state: 'waiting', created: Date.now(), name: '', company: '', purpose: '' });
  // Clean old sessions (>30 min)
  for (const [k, v] of checkinSessions) {
    if (Date.now() - v.created > 30 * 60 * 1000) checkinSessions.delete(k);
  }
  return checkinSessions.get(id);
}

// --- LIDAR Mapper ---
const mapper = new OhmniMapper();
mapper.sendCommand = sendCommand;

// Direct LIDAR reader — reads raw serial data from /dev/ttyUSB0 via ADB
// and decodes RPLidar A2M8 express scan packets. This bypasses the robot's
// broken serialport module and gives us real 360-degree scan data.
const lidarReader = new LidarReader(ROBOT_ADB_ADDR, '/dev/ttyUSB0');
let lidarScanListener = null;

function startLidarPolling() {
  if (lidarScanListener) return;
  // Start the direct serial reader
  lidarReader.start();
  console.log('LiDAR direct reader started on /dev/ttyUSB0');
  // Feed each complete revolution into the mapper
  lidarScanListener = (scan) => {
    if (!mapper.isMapping) return;
    mapper.integrateScan(scan);
  };
  lidarReader.on('scan', lidarScanListener);
}

function stopLidarPolling() {
  if (lidarScanListener) {
    lidarReader.removeListener('scan', lidarScanListener);
    lidarScanListener = null;
  }
  lidarReader.stop();
  console.log('LiDAR direct reader stopped');
}

// --- Camera via ADB + v4l2-ctl (direct V4L2 MJPEG capture) ---
const ROBOT_ADB = ROBOT_ADB_ADDR;
// Camera names to match (device numbers can change after USB re-enumeration)
const CAMERA_NAMES = {
  0: { match: 'See3CAM', res: '640x480', name: 'Screen Camera' },
  1: { match: 'HD USB Camera', res: '640x480', name: 'Floor Camera' },
};
// Resolved device paths (populated by detectCameras)
const CAMERAS = {};

async function detectCameras() {
  // Ensure root access for camera devices
  await new Promise((resolve) => {
    const p = spawn('adb', ['-s', ROBOT_ADB, 'root']);
    p.on('close', () => { setTimeout(resolve, 1000); });
    setTimeout(() => { try { p.kill(); } catch {} resolve(); }, 3000);
  });
  return new Promise((resolve) => {
    const adb = spawn('adb', ['-s', ROBOT_ADB, 'shell',
      'for d in /dev/video*; do echo "DEV:$d"; v4l2-ctl --device=$d --all 2>/dev/null | grep "Card type"; done']);
    let out = '';
    adb.stdout.on('data', (d) => { out += d.toString(); });
    adb.on('close', () => {
      const lines = out.split('\n');
      let currentDev = null;
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith('DEV:')) currentDev = trimmed.slice(4).trim();
        if (trimmed.includes('Card type') && currentDev) {
          const cardName = trimmed.split(':').slice(1).join(':').trim();
          for (const [id, cfg] of Object.entries(CAMERA_NAMES)) {
            if (cardName.includes(cfg.match)) {
              CAMERAS[id] = { dev: currentDev, res: cfg.res, name: cfg.name };
              console.log(`Camera ${id} (${cfg.name}): ${currentDev} [${cardName}]`);
            }
          }
        }
      }
      if (Object.keys(CAMERAS).length === 0) {
        console.warn('No cameras detected, using defaults');
        CAMERAS[0] = { dev: '/dev/video0', res: '640x480', name: 'Screen Camera' };
        CAMERAS[1] = { dev: '/dev/video1', res: '640x480', name: 'Floor Camera' };
      }
      resolve();
    });
    setTimeout(() => { try { adb.kill(); } catch {} }, 5000);
  });
}

function captureFrame(camId = 1) {
  const cam = CAMERAS[camId];
  if (!cam) return Promise.reject(new Error(`Unknown camera ${camId}`));
  return new Promise((resolve, reject) => {
    const adb = spawn('adb', [
      '-s', ROBOT_ADB, 'exec-out',
      'v4l2-ctl', `--device=${cam.dev}`,
      `--set-fmt-video=width=${cam.res.split('x')[0]},height=${cam.res.split('x')[1]},pixelformat=MJPG`,
      '--stream-mmap', '--stream-count=1', '--stream-to=-'
    ]);
    let buf = Buffer.alloc(0);
    adb.stdout.on('data', (chunk) => { buf = Buffer.concat([buf, chunk]); });
    adb.stderr.on('data', () => {});
    adb.on('close', () => {
      const frame = extractJpeg(buf);
      if (frame) resolve(frame);
      else reject(new Error('No frame captured'));
    });
    setTimeout(() => { try { adb.kill(); } catch {} }, 5000);
  });
}

function extractJpeg(buf) {
  let start = -1, end = -1;
  for (let i = 0; i < buf.length - 1; i++) {
    if (buf[i] === 0xff && buf[i + 1] === 0xd8) { start = i; break; }
  }
  if (start >= 0) {
    for (let i = start + 2; i < buf.length - 1; i++) {
      if (buf[i] === 0xff && buf[i + 1] === 0xd9) { end = i + 2; break; }
    }
  }
  return (start >= 0 && end > start) ? buf.slice(start, end) : null;
}

// --- MJPEG stream: persistent v4l2-ctl process per camera ---
const streams = {
  0: { clients: new Set(), proc: null },
  1: { clients: new Set(), proc: null },
};

function startMjpegStream(camId) {
  const stream = streams[camId];
  const cam = CAMERAS[camId];
  if (!stream || !cam || stream.proc) return;
  console.log(`Starting MJPEG stream for camera ${camId} (${cam.name})...`);

  const adb = spawn('adb', [
    '-s', ROBOT_ADB, 'exec-out',
    'v4l2-ctl', `--device=${cam.dev}`,
    `--set-fmt-video=width=${cam.res.split('x')[0]},height=${cam.res.split('x')[1]},pixelformat=MJPG`,
    '--stream-mmap', '--stream-count=0', '--stream-to=-'
  ]);
  stream.proc = adb;

  let buf = Buffer.alloc(0);
  adb.stdout.on('data', (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    while (true) {
      let start = -1, end = -1;
      for (let i = 0; i < buf.length - 1; i++) {
        if (buf[i] === 0xff && buf[i + 1] === 0xd8) { start = i; break; }
      }
      if (start < 0) break;
      for (let i = start + 2; i < buf.length - 1; i++) {
        if (buf[i] === 0xff && buf[i + 1] === 0xd9) { end = i + 2; break; }
      }
      if (end < 0) break;

      const frame = buf.slice(start, end);
      buf = buf.slice(end);
      for (const client of stream.clients) {
        try {
          client.write(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${frame.length}\r\n\r\n`);
          client.write(frame);
          client.write('\r\n');
        } catch {
          stream.clients.delete(client);
        }
      }
    }
  });

  adb.stderr.on('data', () => {});
  adb.on('close', () => {
    console.log(`MJPEG stream ${camId} exited`);
    stream.proc = null;
    if (stream.clients.size > 0) {
      setTimeout(() => startMjpegStream(camId), 500);
    }
  });
}

function stopMjpegStream(camId) {
  const stream = streams[camId];
  if (stream && stream.proc) {
    try { stream.proc.kill(); } catch {}
    stream.proc = null;
    console.log(`MJPEG stream ${camId} stopped (no clients)`);
  }
}

// --- Robot Audio Playback (Android media player via am intent) ---
function startRobotAudio() {
  spawn('adb', ['-s', ROBOT_ADB, 'shell',
    'am', 'start', '-a', 'android.intent.action.VIEW',
    '-d', 'file:///sdcard/salsa.mp3', '-t', 'audio/mpeg']);
}

function stopRobotAudio() {
  // Send media stop key event and kill media player
  spawn('adb', ['-s', ROBOT_ADB, 'shell', 'input', 'keyevent', '86']); // KEYCODE_MEDIA_STOP
  spawn('adb', ['-s', ROBOT_ADB, 'shell', 'am', 'force-stop', 'com.android.music']);
  spawn('adb', ['-s', ROBOT_ADB, 'shell', 'am', 'force-stop', 'com.google.android.music']);
}

// --- Salsa Dance Choreography ---
let danceRunning = false;

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function runSalsaDance() {
  // Salsa tempo ~170 BPM = ~350ms per beat
  const beat = 350;

  // Helper to fire-and-forget commands (don't wait for response)
  function cmd(c) { if (danceRunning && botSocket && !botSocket.destroyed) botSocket.write(c + '\n'); }

  console.log('Salsa dance started!');

  // Announce, then start music after TTS finishes
  cmd('say bailamos salsa!');
  await sleep(3000);
  if (!danceRunning) return;
  startRobotAudio();

  // Salsa dance loop - 8-beat patterns
  while (danceRunning) {
    // === Pattern 1: Basic salsa step (forward-back with head bob) ===
    cmd('light_color 20 0 255 255');       // Red LED
    cmd('pre_drive 80 8');                  // Step forward
    cmd('neck_angle 560');                  // Head up
    await sleep(beat);

    cmd('neck_angle 460');                  // Head down
    await sleep(beat);

    cmd('pre_drive -80 8');                 // Step back
    cmd('neck_angle 560');                  // Head up
    await sleep(beat);

    cmd('neck_angle 460');                  // Head down
    await sleep(beat);

    if (!danceRunning) break;

    // Beats 5-8: Side step with rotation
    cmd('light_color 20 40 255 255');       // Orange LED
    cmd('pre_rot 30 8');                    // Rotate left
    cmd('neck_angle 540');
    await sleep(beat);

    cmd('neck_angle 480');
    await sleep(beat);

    cmd('pre_rot -30 8');                   // Rotate right
    cmd('neck_angle 540');
    await sleep(beat);

    cmd('neck_angle 512');                  // Center
    await sleep(beat);

    if (!danceRunning) break;

    // === Pattern 2: Cumbia-style spin with LED rainbow ===
    cmd('light_color 20 85 255 255');       // Green LED
    cmd('pre_rot 60 10');                   // Spin left
    cmd('neck_angle 600');                  // Head way up
    await sleep(beat * 2);

    cmd('light_color 20 170 255 255');      // Blue LED
    cmd('pre_rot -60 10');                  // Spin right
    cmd('neck_angle 400');                  // Head way down
    await sleep(beat * 2);

    if (!danceRunning) break;

    cmd('light_color 20 213 255 255');      // Purple LED
    cmd('pre_drive 100 10');                // Charge forward
    cmd('neck_angle 560');
    await sleep(beat * 2);

    cmd('light_color 20 0 255 255');        // Red LED
    cmd('pre_drive -100 10');               // Back up
    cmd('neck_angle 460');
    await sleep(beat * 2);

    if (!danceRunning) break;

    // === Pattern 3: Shimmy (rapid small rotations + head bobs) ===
    for (let i = 0; i < 4 && danceRunning; i++) {
      cmd(`light_color 20 ${i * 64} 255 255`);
      cmd('pre_rot 15 12');
      cmd(i % 2 === 0 ? 'neck_angle 550' : 'neck_angle 470');
      await sleep(beat / 2);
      cmd('pre_rot -15 12');
      cmd(i % 2 === 0 ? 'neck_angle 470' : 'neck_angle 550');
      await sleep(beat / 2);
    }

    if (!danceRunning) break;

    // === Pattern 4: Big salsa turn ===
    cmd('light_color 20 0 255 255');
    cmd('pre_rot 180 8');                   // Full spin!
    cmd('neck_angle 600');
    await sleep(beat * 4);

    cmd('neck_angle 512');
    cmd('light_color 20 128 255 255');
    await sleep(beat * 2);

    // Brief pause between loops
    cmd('manual_move 0 0');
    cmd('neck_angle 512');
    await sleep(beat * 2);
  }

  // Cleanup
  cmd('manual_move 0 0');
  cmd('neck_angle 512');
  cmd('light_color 20 0 0 0');
  danceRunning = false;
  console.log('Salsa dance ended');
}

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') { res.writeHead(200); res.end(); return; }

  const urlPath = req.url.split('?')[0];

  // --- MJPEG live stream endpoint: /api/stream/0, /api/stream/1, /api/stream (default=1) ---
  const streamMatch = urlPath.match(/^\/api\/stream(?:\/([01]))?$/);
  if (streamMatch) {
    const camId = parseInt(streamMatch[1] ?? '1');
    const stream = streams[camId];
    res.writeHead(200, {
      'Content-Type': 'multipart/x-mixed-replace; boundary=frame',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
    });
    stream.clients.add(res);
    req.on('close', () => {
      stream.clients.delete(res);
      if (stream.clients.size === 0) stopMjpegStream(camId);
    });
    startMjpegStream(camId);
    return;
  }

  // --- Single snapshot endpoint: /api/snapshot/0, /api/snapshot/1, /api/snapshot (default=1) ---
  const snapMatch = urlPath.match(/^\/api\/snapshot(?:\/([01]))?$/);
  if (snapMatch) {
    const camId = parseInt(snapMatch[1] ?? '1');
    try {
      const frame = await captureFrame(camId);
      res.writeHead(200, { 'Content-Type': 'image/jpeg', 'Content-Length': frame.length });
      res.end(frame);
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
    return;
  }

  // --- Raw command ---
  if (req.url === '/api/command' && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', async () => {
      try {
        const { cmd } = JSON.parse(body);
        const response = await sendCommand(cmd);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, response: response.trim() }));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
    return;
  }

  if (req.url === '/api/battery') {
    const response = await sendCommand('battery');
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, response: response.trim() }));
    return;
  }

  if (req.url === '/api/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      connected: botSocket && !botSocket.destroyed,
      adbConnected,
      robot: 'Ohmni 5-2',
      ip: ROBOT_IP,
      streamClients: { cam0: streams[0].clients.size, cam1: streams[1].clients.size }
    }));
    return;
  }

  // --- USB Device Enumeration ---
  if (req.url === '/api/usb-devices') {
    try {
      const devices = await getUsbDevices();
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, devices }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: e.message }));
    }
    return;
  }

  // --- LIDAR Status ---
  if (req.url === '/api/lidar') {
    try {
      const status = await getLidarStatus();
      const directReader = {
        running: lidarReader.running,
        scanCount: lidarReader.scanCount,
        lastScanSize: lidarReader.lastScan.length,
        obstacles: lidarReader.getObstacleDistances(),
        bufferSize: lidarReader.buffer.length,
      };
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, adbConnected, status, directReader }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: e.message }));
    }
    return;
  }

  // --- LIDAR Config (collision detection settings via bot_shell) ---
  if (req.url === '/api/lidar/config' && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', async () => {
      try {
        const config = JSON.parse(body);
        const isInFront = config.isInFront ? '1' : '0';
        const cmd = `update_lidar_config ${isInFront} ${config.frontWarningDistance || 1250} ${config.frontStoppingDistance || 750} ${config.backWarningDistance || 1350} ${config.backStoppingDistance || 550} ${config.lidarRotationSpeed || 660}`;
        const response = await sendCommand(cmd);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, response: response.trim(), config }));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
    return;
  }

  // --- LIDAR control commands ---
  if (req.url === '/api/lidar/scan-device' && req.method === 'POST') {
    const r1 = await sendCommand('scan_lidar_device');
    const r2 = await sendCommand('start_collision_detection');
    const r3 = await sendCommand('lidar_scan');
    const r4 = await sendCommand('toggle_collision_detection on');
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, responses: [r1, r2, r3, r4].map(r => r.trim()) }));
    return;
  }

  if (req.url === '/api/lidar/stop' && req.method === 'POST') {
    const r1 = await sendCommand('lidar_stop');
    const r2 = await sendCommand('toggle_collision_detection off');
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, responses: [r1, r2].map(r => r.trim()) }));
    return;
  }

  // --- ADB reconnect trigger ---
  if (req.url === '/api/reconnect' && req.method === 'POST') {
    const ok = await ensureAdbConnection();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok, adbConnected }));
    return;
  }

  // --- Mapping endpoints ---
  if (req.url === '/api/map/start' && req.method === 'POST') {
    // Start the direct LiDAR serial reader (reads raw data via ADB)
    // Also init the robot's collision detection for autostop safety
    sendCommand('scan_lidar_device').then(() =>
      sendCommand('start_collision_detection').then(() =>
        sendCommand('toggle_collision_detection on')
      )
    ).catch(() => {});
    mapper.startMapping();
    startLidarPolling();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, status: 'mapping started' }));
    return;
  }

  if (req.url === '/api/map/stop' && req.method === 'POST') {
    mapper.stopMapping();
    stopLidarPolling();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, status: 'mapping stopped' }));
    return;
  }

  if (req.url === '/api/map/reset' && req.method === 'POST') {
    mapper.resetMap();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, status: 'map reset' }));
    return;
  }

  if (req.url === '/api/map/explore' && req.method === 'POST') {
    mapper.startExploring();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, status: 'exploring started' }));
    return;
  }

  if (req.url === '/api/map/explore/stop' && req.method === 'POST') {
    mapper.stopExploring();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, status: 'exploring stopped' }));
    return;
  }

  if (req.url === '/api/map/state') {
    const state = mapper.getMapState();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(state));
    return;
  }

  // Feed LIDAR scan data into mapper (called from UI or external source)
  if (req.url === '/api/map/scan' && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const { readings, pose } = JSON.parse(body);
        if (pose) {
          if (pose.forward) mapper.updatePoseForward(pose.forward);
          if (pose.rotate) mapper.updatePoseRotation(pose.rotate);
        }
        if (readings && readings.length > 0) {
          mapper.integrateScan(readings);
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, stats: mapper.stats }));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
    return;
  }

  // --- Salsa dance endpoint ---
  if (urlPath === '/api/dance' && req.method === 'POST') {
    if (danceRunning) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, action: 'already dancing' }));
      return;
    }
    danceRunning = true;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, action: 'started' }));
    runSalsaDance();
    return;
  }

  if (urlPath === '/api/dance/stop' && req.method === 'POST') {
    danceRunning = false;
    stopRobotAudio();
    sendCommand('manual_move 0 0');
    sendCommand('neck_angle 512');
    sendCommand('light_color 20 0 0 0');
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, action: 'stopped' }));
    return;
  }

  // --- Movement with throttle ---
  if (req.url.startsWith('/api/move') && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', async () => {
      const { action, speed = 8, distance = 200, angle = 45, mode = 'tap' } = JSON.parse(body);
      const now = Date.now();

      let cmd;

      if (mode === 'continuous') {
        // Continuous mode: use manual_move for sustained velocity
        const linearSpeed = Math.round(speed * 25);  // slider 3-18 -> 75-450
        const angularSpeed = Math.round(speed * 20);  // slider 3-18 -> 60-360
        switch (action) {
          case 'forward': cmd = `manual_move ${linearSpeed} 0`; break;
          case 'backward': cmd = `manual_move -${linearSpeed} 0`; break;
          case 'left': cmd = `manual_move 0 ${angularSpeed}`; break;
          case 'right': cmd = `manual_move 0 -${angularSpeed}`; break;
          case 'stop': cmd = 'manual_move 0 0'; break;
        }
      }

      if (!cmd) {
        // Tap mode (single step) or head commands
        if (mode === 'tap' && action !== 'stop' && now - lastMoveTime < 500) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: true, cmd: 'throttled', response: 'throttled' }));
          return;
        }
        if (mode === 'tap') lastMoveTime = now;

        switch (action) {
          case 'forward': cmd = `pre_drive ${distance} ${speed}`; break;
          case 'backward': cmd = `pre_drive -${distance} ${speed}`; break;
          case 'left': cmd = `pre_rot ${angle} ${speed}`; break;
          case 'right': cmd = `pre_rot -${angle} ${speed}`; break;
          case 'stop': cmd = 'manual_move 0 0'; break;
          case 'head_up': cmd = 'neck_angle 600'; break;
          case 'head_down': cmd = 'neck_angle 400'; break;
          case 'head_center': cmd = 'neck_angle 512'; break;
          default:
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Unknown action' }));
            return;
        }
      }

      console.log(`> ${cmd} (${mode})`);
      const response = await sendCommand(cmd);

      // Update mapper pose tracking
      if (mapper.isMapping) {
        if (action === 'forward') mapper.updatePoseForward(distance);
        else if (action === 'backward') mapper.updatePoseForward(-distance);
        else if (action === 'left') mapper.updatePoseRotation(-angle);
        else if (action === 'right') mapper.updatePoseRotation(angle);
      }

      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, cmd, response: response.trim() }));
    });
    return;
  }

  // --- Check-in System (QR code + phone flow) ---

  // Create a new session (robot screen calls this to get a QR code URL)
  if (req.url === '/api/checkin/session' && req.method === 'POST') {
    const session = createSession();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, session }));
    return;
  }

  // Poll session state (both robot screen and phone poll this)
  const sessionPollMatch = req.url.match(/^\/api\/checkin\/session\/(\w+)$/);
  if (sessionPollMatch && req.method === 'GET') {
    const session = checkinSessions.get(sessionPollMatch[1]);
    if (!session) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'Session expired' }));
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, session }));
    }
    return;
  }

  // Phone submits visitor details to session
  if (req.url.match(/^\/api\/checkin\/session\/\w+\/submit$/) && req.method === 'POST') {
    const sid = req.url.split('/')[4];
    const session = checkinSessions.get(sid);
    if (!session) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'Session expired' }));
      return;
    }
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const { name, company, purpose } = JSON.parse(body);
        if (!name || name.trim().length < 2) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: 'Name is required' }));
          return;
        }
        session.name = name.trim();
        session.company = (company || '').trim();
        session.purpose = (purpose || '').trim();
        session.state = 'submitted';
        console.log(`Check-in session ${sid}: ${session.name} submitted from phone`);

        // Wake head and position for photo
        sendCommand('wake_head');
        setTimeout(() => sendCommand(`neck_angle ${checkinNeckPos}`), 300);

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, session }));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
    return;
  }

  // Robot screen triggers photo capture for a session
  if (req.url.match(/^\/api\/checkin\/session\/\w+\/capture$/) && req.method === 'POST') {
    const sid = req.url.split('/')[4];
    const session = checkinSessions.get(sid);
    if (!session) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'Session expired' }));
      return;
    }
    try {
      const frame = await captureFrame(0); // Front camera
      const photosDir = path.join(__dirname, 'checkin-photos');
      if (!fs.existsSync(photosDir)) fs.mkdirSync(photosDir, { recursive: true });
      const photoFile = `${sid}.jpg`;
      fs.writeFileSync(path.join(photosDir, photoFile), frame);

      // Save the check-in record
      const record = {
        id: sid, name: session.name, company: session.company,
        purpose: session.purpose, timestamp: new Date().toISOString(), photoFile
      };
      const checkins = loadCheckins();
      checkins.push(record);
      saveCheckins(checkins);

      session.state = 'done';
      session.photoFile = photoFile;
      console.log(`Check-in complete: ${session.name} — photo saved`);

      // Robot says welcome
      sendCommand(`say Welcome ${session.name.split(' ')[0]}! Please have a seat.`);

      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, record }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: e.message }));
    }
    return;
  }

  // Neck adjust (face centering)
  if (req.url === '/api/checkin/adjust-neck' && req.method === 'POST') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', async () => {
      try {
        const { direction } = JSON.parse(body);
        checkinNeckPos += (direction === 'up' ? 30 : -30);
        checkinNeckPos = Math.max(300, Math.min(600, checkinNeckPos));
        await sendCommand(`neck_angle ${checkinNeckPos}`);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, neck: checkinNeckPos }));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    });
    return;
  }

  // List all check-ins (admin)
  if (req.url === '/api/checkins') {
    const checkins = loadCheckins();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, checkins }));
    return;
  }

  // Serve check-in photos
  const photoMatch = req.url.match(/^\/api\/checkin\/photo\/(.+\.jpg)$/);
  if (photoMatch) {
    const photoPath = path.join(__dirname, 'checkin-photos', photoMatch[1]);
    try {
      const photo = fs.readFileSync(photoPath);
      res.writeHead(200, { 'Content-Type': 'image/jpeg', 'Content-Length': photo.length });
      res.end(photo);
    } catch {
      res.writeHead(404); res.end('Not found');
    }
    return;
  }

  // Delete a check-in (admin)
  const deleteMatch = req.url.match(/^\/api\/checkin\/(\w+)$/) ;
  if (deleteMatch && req.method === 'DELETE') {
    const id = deleteMatch[1];
    const checkins = loadCheckins();
    const idx = checkins.findIndex(c => c.id === id);
    if (idx >= 0) {
      const removed = checkins.splice(idx, 1)[0];
      saveCheckins(checkins);
      if (removed.photoFile) {
        try { fs.unlinkSync(path.join(__dirname, 'checkin-photos', removed.photoFile)); } catch {}
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    } else {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'Not found' }));
    }
    return;
  }

  // Capture a photo snapshot (direct)
  if (req.url === '/api/checkin/capture') {
    try {
      const frame = await captureFrame(0);
      res.writeHead(200, { 'Content-Type': 'image/jpeg', 'Content-Length': frame.length });
      res.end(frame);
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
    return;
  }

  // --- Static files ---
  let filePath = req.url === '/' ? '/index.html' : req.url;
  filePath = path.join(__dirname, 'public', filePath);
  const ext = path.extname(filePath);
  const contentTypes = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.mp3': 'audio/mpeg' };
  try {
    const content = fs.readFileSync(filePath);
    res.writeHead(200, { 'Content-Type': contentTypes[ext] || 'text/plain' });
    res.end(content);
  } catch {
    res.writeHead(404);
    res.end('Not found');
  }
});

detectCameras().then(() => {
  server.listen(PORT, () => {
    console.log(`Ohmni Control Panel running at http://localhost:${PORT}`);
  });
});
