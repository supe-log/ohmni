// lidar_reader.js — Direct RPLidar A2M8 express scan reader via ADB serial
// Reads raw serial data from /dev/ttyUSB0 through ADB, decodes express scan
// packets, and emits angle/distance pairs for the mapper.
//
// This bypasses the robot's node process (which owns the serial port but
// whose serialport module doesn't reliably relay data). On Linux/Android,
// serial ports allow multiple readers by default.

const { spawn } = require('child_process');
const { EventEmitter } = require('events');

const PACKET_SIZE = 84; // Express scan packet: 4 header + 80 cabin data
const SYNC1 = 0xA;
const SYNC2 = 0x5;
const MIN_DISTANCE = 150; // mm — filter noise
const MAX_DISTANCE = 8000; // mm — LiDAR max range

// Dead zones: angles where the LiDAR sees the robot's own structure.
// LiDAR is front-mounted on the robot base. The laser beam catches:
// - The pole behind at ~178-186° at ~182mm
// - The robot's own base edges at ~182mm at various angles
// Since the robot body reads at a characteristic ~182mm (±10mm) from
// almost any angle, we filter ALL readings in the 170-200mm band.
// Real obstacles at exactly that distance are rare and the robot's
// collision detection handles close-range safety.
// Robot body signature: exactly 182mm ±5mm from any angle
const BODY_DIST_MIN = 177;
const BODY_DIST_MAX = 190;

const DEAD_ZONES = [];

function isInDeadZone(angle, distance) {
  // Filter the robot's own body signature: consistent ~182mm readings
  if (distance >= BODY_DIST_MIN && distance <= BODY_DIST_MAX) return true;
  return false;
}

function isInDeadZone(angle, distance) {
  for (const zone of DEAD_ZONES) {
    if (angle >= zone.minAngle && angle <= zone.maxAngle && distance < zone.maxDist) {
      return true;
    }
  }
  return false;
}

class LidarReader extends EventEmitter {
  constructor(adbAddr, serialPort = '/dev/ttyUSB0') {
    super();
    this.adbAddr = adbAddr;
    this.serialPort = serialPort;
    this.process = null;
    this.buffer = Buffer.alloc(0);
    this.running = false;
    this.prevPacket = null; // Need two consecutive packets for angle calculation
    this.scanCount = 0;
    this.lastScan = []; // Latest full revolution of readings
    this._currentScan = [];
    this._lastStartAngle = -1;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this._spawn();
  }

  stop() {
    this.running = false;
    if (this.process) {
      try { this.process.kill(); } catch {}
      this.process = null;
    }
  }

  _spawn() {
    if (!this.running) return;

    this.process = spawn('adb', [
      '-s', this.adbAddr, 'shell',
      `cat ${this.serialPort}`
    ]);

    this.process.stdout.on('data', (data) => {
      this._onData(data);
    });

    this.process.stderr.on('data', (data) => {
      // Silently ignore stderr
    });

    this.process.on('close', (code) => {
      this.process = null;
      if (this.running) {
        // Auto-restart after 2 seconds
        setTimeout(() => this._spawn(), 2000);
      }
    });

    this.process.on('error', (err) => {
      console.log('LidarReader: spawn error:', err.message);
      this.process = null;
      if (this.running) {
        setTimeout(() => this._spawn(), 2000);
      }
    });
  }

  _onData(data) {
    // Append to buffer
    this.buffer = Buffer.concat([this.buffer, data]);

    // Process all complete packets in the buffer
    while (this.buffer.length >= PACKET_SIZE) {
      // Find sync header: upper nibble of byte 0 = 0xA, upper nibble of byte 1 = 0x5
      const sync1 = (this.buffer[0] & 0xF0) >> 4;
      const sync2 = (this.buffer[1] & 0xF0) >> 4;

      if (sync1 !== SYNC1 || sync2 !== SYNC2) {
        // Not a valid packet start — scan forward to find sync
        let found = false;
        for (let i = 1; i <= this.buffer.length - 2; i++) {
          if (((this.buffer[i] & 0xF0) >> 4) === SYNC1 &&
              ((this.buffer[i + 1] & 0xF0) >> 4) === SYNC2) {
            this.buffer = this.buffer.subarray(i);
            found = true;
            break;
          }
        }
        if (!found) {
          // No sync found, keep last byte in case it's start of sync
          this.buffer = this.buffer.subarray(this.buffer.length - 1);
        }
        continue;
      }

      if (this.buffer.length < PACKET_SIZE) break;

      // Verify checksum
      const rcvCs = (this.buffer[0] & 0x0F) | ((this.buffer[1] & 0x0F) << 4);
      let calcCs = 0;
      for (let i = 2; i < PACKET_SIZE; i++) {
        calcCs ^= this.buffer[i];
      }

      if (rcvCs !== calcCs) {
        // Bad checksum — skip 2 bytes and resync
        this.buffer = this.buffer.subarray(2);
        continue;
      }

      // Valid packet — extract it
      const packet = Buffer.from(this.buffer.subarray(0, PACKET_SIZE));
      this.buffer = this.buffer.subarray(PACKET_SIZE);

      this._processPacket(packet);
    }

    // Prevent buffer from growing unbounded
    if (this.buffer.length > PACKET_SIZE * 100) {
      this.buffer = this.buffer.subarray(this.buffer.length - PACKET_SIZE * 10);
    }
  }

  _processPacket(packet) {
    // Extract start angle (q6 format: angle_degrees = value / 64.0)
    const startAngleQ6 = (packet[2] | ((packet[3] & 0x7F) << 8));
    const startAngle = startAngleQ6 / 64.0;

    // Need previous packet to compute inter-packet angle difference
    if (!this.prevPacket) {
      this.prevPacket = { raw: packet, startAngle };
      return;
    }

    const prevPacket = this.prevPacket.raw;
    const prevStartAngle = this.prevPacket.startAngle;
    const nextStartAngle = startAngle;

    // Compute angle difference between packets
    let angleDiff = nextStartAngle - prevStartAngle;
    if (angleDiff < 0) angleDiff += 360;

    // Process 16 cabins from the PREVIOUS packet (need current packet's angle)
    const readings = [];
    for (let cabinIdx = 0; cabinIdx < 16; cabinIdx++) {
      const offset = 4 + cabinIdx * 5;

      // Distance values (14-bit, in mm)
      const d1 = ((prevPacket[offset] & 0xFC) >> 2) | (prevPacket[offset + 1] << 6);
      const d2 = ((prevPacket[offset + 2] & 0xFC) >> 2) | (prevPacket[offset + 3] << 6);

      // Delta angle (q3 format with sign)
      const theta1Q3 = ((prevPacket[offset] & 0x03) << 4) | (prevPacket[offset + 4] & 0x0F);
      let theta1 = (theta1Q3 & 0x1F) / 8.0;
      if (theta1Q3 >> 5) theta1 = -theta1;

      const theta2Q3 = ((prevPacket[offset + 2] & 0x03) << 4) | ((prevPacket[offset + 4] & 0xF0) >> 4);
      let theta2 = (theta2Q3 & 0x1F) / 8.0;
      if (theta2Q3 >> 5) theta2 = -theta2;

      // Compute actual angles
      const k = cabinIdx;
      // Interpolate angle within this packet's angular span
      const angleInterp1 = angleDiff * (k * 2) / 32.0;
      const angleInterp2 = angleDiff * (k * 2 + 1) / 32.0;

      let angle1 = prevStartAngle + angleInterp1 - theta1;
      let angle2 = prevStartAngle + angleInterp2 - theta2;

      // Normalize angles to [0, 360)
      while (angle1 < 0) angle1 += 360;
      while (angle1 >= 360) angle1 -= 360;
      while (angle2 < 0) angle2 += 360;
      while (angle2 >= 360) angle2 -= 360;

      // Only emit valid distance readings, filtering robot's own structure
      if (d1 > 0 && !isInDeadZone(angle1, d1)) readings.push({ angle: angle1, distance: d1 });
      if (d2 > 0 && !isInDeadZone(angle2, d2)) readings.push({ angle: angle2, distance: d2 });
    }

    // Detect revolution boundary (start angle wraps around)
    if (prevStartAngle > 270 && startAngle < 90) {
      // Complete revolution — emit the accumulated scan
      if (this._currentScan.length > 10) {
        this.lastScan = this._currentScan;
        this.scanCount++;
        this.emit('scan', this.lastScan);
      }
      this._currentScan = [];
    }

    // Accumulate readings into current scan
    this._currentScan.push(...readings);

    // Save current packet as previous for next iteration
    this.prevPacket = { raw: packet, startAngle };
  }

  // Get latest complete 360-degree scan
  getLatestScan() {
    return this.lastScan;
  }

  // Get filtered scan (remove noise, out-of-range)
  getFilteredScan() {
    return this.lastScan.filter(r =>
      r.distance >= MIN_DISTANCE && r.distance <= MAX_DISTANCE
    );
  }

  // Get obstacle distances per quadrant (relative to LiDAR frame)
  // LiDAR now FRONT-mounted, rotated 180° from original:
  //   0° = robot front, 180° = robot back
  //   90° = robot left,  270° = robot right
  getObstacleDistances() {
    const obstacles = { front: 8000, back: 8000, left: 8000, right: 8000 };
    const scan = this.lastScan;

    for (const { angle, distance } of scan) {
      if (distance < MIN_DISTANCE || distance > MAX_DISTANCE) continue;

      if (angle >= 315 || angle <= 45) {
        // Front (0° center)
        if (distance < obstacles.front) obstacles.front = distance;
      } else if (angle >= 135 && angle <= 225) {
        // Back (180° center)
        if (distance < obstacles.back) obstacles.back = distance;
      } else if (angle > 45 && angle < 135) {
        // Left (90° center)
        if (distance < obstacles.left) obstacles.left = distance;
      } else {
        // Right (270° center)
        if (distance < obstacles.right) obstacles.right = distance;
      }
    }

    return obstacles;
  }
}

module.exports = { LidarReader };
