// mapper.js — LIDAR occupancy grid mapper + autonomous explorer for Ohmni robot
// Collects LIDAR scans via bot_shell, tracks pose via dead reckoning,
// builds an occupancy grid, and provides frontier-based autonomous exploration.

const { EventEmitter } = require('events');

// Robot physical constants (from Ohmni firmware)
const WHEEL_DIA_MM = 150.21;
const BASE_DIA_MM = 330.0;
const GEAR_RATIO = 30 / 11;
const ENCODER_BITS = 14; // 16384 ticks/rev
const TICKS_PER_REV = (1 << ENCODER_BITS) * GEAR_RATIO;
const MM_PER_TICK = (Math.PI * WHEEL_DIA_MM) / TICKS_PER_REV;
const BASE_CIRCUMFERENCE = Math.PI * BASE_DIA_MM;

// Map constants
const CELL_SIZE_MM = 50;       // 50mm per cell = 5cm resolution
const MAP_SIZE_CELLS = 400;    // 400x400 = 20m x 20m
const MAP_ORIGIN = MAP_SIZE_CELLS / 2; // Robot starts at center
const LIDAR_MAX_RANGE_MM = 6000;
const LIDAR_MIN_RANGE_MM = 150;

// Dead zones: LiDAR front-mounted on robot base.
// The robot body reads at a characteristic ~182mm (±10mm) from many angles.
function isRobotStructure(angle, distance) {
  if (distance >= 177 && distance <= 190) return true;
  return false;
}

// Occupancy values (display thresholds for the log-odds grid)
const UNKNOWN = 128;
const FREE = 0;
const OCCUPIED = 255;

// Log-odds occupancy: each cell stores a value 0-255 representing probability.
// 128 = unknown (prior), <128 = likely free, >128 = likely occupied.
// Each ray decrements cells it passes through; endpoints get incremented.
// This lets phantom walls from pose drift naturally decay.
const LOG_ODDS_FREE = -3;     // Evidence of free space per ray pass-through
const LOG_ODDS_OCCUPIED = 8;  // Evidence of occupied per hit
const LOG_ODDS_MIN = 10;      // Clamp: very confident free
const LOG_ODDS_MAX = 240;     // Clamp: very confident occupied
const OCCUPIED_THRESHOLD = 170; // Above this = render as wall
const FREE_THRESHOLD = 100;     // Below this = render as free

// Exploration constants
const EXPLORE_SPEED = 4;          // pre_drive speed parameter (slower = safer)
const EXPLORE_STEP_MM = 100;      // mm per forward step (shorter = safer in tight spaces)
const EXPLORE_ROT_DEG = 30;       // degrees per rotation step
const SAFE_DISTANCE_MM = 220;     // min distance before stopping (body edge is ~182mm from sensor, 38mm clearance)
const WARNING_DISTANCE_MM = 400;  // slow down / shorter steps

class OhmniMapper extends EventEmitter {
  constructor() {
    super();

    // Robot pose (mm, radians) — starts at map center facing "up" (+Y)
    this.pose = { x: 0, y: 0, theta: 0 };

    // Occupancy grid: 0=free, 128=unknown, 255=occupied
    this.grid = Buffer.alloc(MAP_SIZE_CELLS * MAP_SIZE_CELLS, UNKNOWN);

    // LIDAR scan data — latest full revolution
    this.lidarScan = []; // [{angle, distance}, ...]

    // Mapping state
    this.isMapping = false;
    this.isExploring = false;
    this.explorerInterval = null;

    // Path history for visualization
    this.path = [{ x: 0, y: 0 }];

    // Stats
    this.stats = {
      scans: 0,
      cellsExplored: 0,
      cellsOccupied: 0,
      distanceTravelled: 0,
    };

    // Command callback — set externally to send bot_shell commands
    this.sendCommand = null;

    // Latest obstacle distances per quadrant for navigation
    this.obstacles = { front: 8000, back: 8000, left: 8000, right: 8000 };
  }

  // --- Grid helpers ---
  worldToGrid(wx, wy) {
    return {
      gx: Math.floor(wx / CELL_SIZE_MM) + MAP_ORIGIN,
      gy: Math.floor(wy / CELL_SIZE_MM) + MAP_ORIGIN,
    };
  }

  gridToWorld(gx, gy) {
    return {
      wx: (gx - MAP_ORIGIN) * CELL_SIZE_MM + CELL_SIZE_MM / 2,
      wy: (gy - MAP_ORIGIN) * CELL_SIZE_MM + CELL_SIZE_MM / 2,
    };
  }

  getCell(gx, gy) {
    if (gx < 0 || gx >= MAP_SIZE_CELLS || gy < 0 || gy >= MAP_SIZE_CELLS) return UNKNOWN;
    return this.grid[gy * MAP_SIZE_CELLS + gx];
  }

  setCell(gx, gy, val) {
    if (gx < 0 || gx >= MAP_SIZE_CELLS || gy < 0 || gy >= MAP_SIZE_CELLS) return;
    this.grid[gy * MAP_SIZE_CELLS + gx] = val;
  }

  // Adjust cell by delta (log-odds update), clamped to [LOG_ODDS_MIN, LOG_ODDS_MAX]
  adjustCell(gx, gy, delta) {
    if (gx < 0 || gx >= MAP_SIZE_CELLS || gy < 0 || gy >= MAP_SIZE_CELLS) return;
    const idx = gy * MAP_SIZE_CELLS + gx;
    const newVal = this.grid[idx] + delta;
    this.grid[idx] = Math.max(LOG_ODDS_MIN, Math.min(LOG_ODDS_MAX, newVal));
  }

  // --- Pose update from movement commands ---
  // Called after pre_drive completes
  updatePoseForward(distMm) {
    this.pose.x += distMm * Math.cos(this.pose.theta);
    this.pose.y += distMm * Math.sin(this.pose.theta);
    this.stats.distanceTravelled += Math.abs(distMm);
    this.path.push({ x: this.pose.x, y: this.pose.y });
  }

  // Called after pre_rot completes
  updatePoseRotation(angleDeg) {
    this.pose.theta += (angleDeg * Math.PI) / 180;
    // Normalize to [-PI, PI]
    while (this.pose.theta > Math.PI) this.pose.theta -= 2 * Math.PI;
    while (this.pose.theta < -Math.PI) this.pose.theta += 2 * Math.PI;
  }

  // --- LIDAR scan integration ---
  // Process a batch of LIDAR readings and update the occupancy grid
  integrateScan(readings) {
    if (!this.isMapping) return;

    this.lidarScan = readings;
    this.stats.scans++;

    const { x: rx, y: ry, theta: rtheta } = this.pose;
    const robotGrid = this.worldToGrid(rx, ry);

    // Reset obstacle distances
    this.obstacles = { front: 8000, back: 8000, left: 8000, right: 8000 };

    for (const { angle, distance } of readings) {
      if (distance < LIDAR_MIN_RANGE_MM || distance > LIDAR_MAX_RANGE_MM) continue;

      // Skip readings from the robot's own structure (pole + base)
      if (isRobotStructure(angle, distance)) continue;

      // LIDAR angle is relative to the sensor; convert to world frame
      // LIDAR now FRONT-mounted: 0° = robot front, 180° = robot back
      const lidarRad = (angle * Math.PI) / 180;
      const worldAngle = rtheta + lidarRad; // 0° = front, no offset needed

      // Endpoint of this ray in world coords
      const endX = rx + distance * Math.cos(worldAngle);
      const endY = ry + distance * Math.sin(worldAngle);
      const endGrid = this.worldToGrid(endX, endY);

      // Classify into quadrant for navigation
      // LiDAR front-mounted: 0°=front, 90°=left, 180°=back, 270°=right
      let relAngle = lidarRad * (180 / Math.PI);
      while (relAngle < 0) relAngle += 360;
      while (relAngle >= 360) relAngle -= 360;

      if (relAngle >= 315 || relAngle <= 45) {
        if (distance < this.obstacles.front) this.obstacles.front = distance;
      } else if (relAngle >= 135 && relAngle <= 225) {
        if (distance < this.obstacles.back) this.obstacles.back = distance;
      } else if (relAngle > 45 && relAngle < 135) {
        if (distance < this.obstacles.left) this.obstacles.left = distance;
      } else {
        if (distance < this.obstacles.right) this.obstacles.right = distance;
      }

      // Ray-cast: decrease log-odds for all cells the ray passes through (evidence of free)
      this.bresenhamLogOdds(robotGrid.gx, robotGrid.gy, endGrid.gx, endGrid.gy);

      // Increase log-odds at endpoint (evidence of occupied)
      if (distance < LIDAR_MAX_RANGE_MM - 100) {
        this.adjustCell(endGrid.gx, endGrid.gy, LOG_ODDS_OCCUPIED);
      }
    }

    // Mark robot's current cell as definitely free
    this.adjustCell(robotGrid.gx, robotGrid.gy, LOG_ODDS_FREE * 3);

    // Update stats (count based on thresholds)
    let explored = 0, occupied = 0;
    for (let i = 0; i < this.grid.length; i++) {
      if (this.grid[i] < FREE_THRESHOLD) explored++;
      else if (this.grid[i] > OCCUPIED_THRESHOLD) occupied++;
    }
    this.stats.cellsExplored = explored;
    this.stats.cellsOccupied = occupied;

    this.emit('scan', { obstacles: this.obstacles, stats: this.stats });
  }

  // Bresenham line with log-odds: decrease confidence for all cells along the ray.
  // This DOES clear stale occupied cells — real walls get re-marked at endpoints,
  // phantom walls from drift gradually fade as rays pass through them.
  bresenhamLogOdds(x0, y0, x1, y1) {
    const dx = Math.abs(x1 - x0);
    const dy = Math.abs(y1 - y0);
    const sx = x0 < x1 ? 1 : -1;
    const sy = y0 < y1 ? 1 : -1;
    let err = dx - dy;

    const maxSteps = dx + dy;
    let steps = 0;

    while (steps < maxSteps) {
      this.adjustCell(x0, y0, LOG_ODDS_FREE);

      if (x0 === x1 && y0 === y1) break;
      const e2 = 2 * err;
      if (e2 > -dy) { err -= dy; x0 += sx; }
      if (e2 < dx) { err += dx; y0 += sy; }
      steps++;
    }
  }

  // Keep old bresenham for compatibility
  bresenham(x0, y0, x1, y1, val) {
    const dx = Math.abs(x1 - x0);
    const dy = Math.abs(y1 - y0);
    const sx = x0 < x1 ? 1 : -1;
    const sy = y0 < y1 ? 1 : -1;
    let err = dx - dy;
    const maxSteps = dx + dy;
    let steps = 0;
    while (steps < maxSteps) {
      this.setCell(x0, y0, val);
      if (x0 === x1 && y0 === y1) break;
      const e2 = 2 * err;
      if (e2 > -dy) { err -= dy; x0 += sx; }
      if (e2 < dx) { err += dx; y0 += sy; }
      steps++;
    }
  }

  // --- Mapping control ---
  startMapping() {
    this.isMapping = true;
    console.log('Mapper: mapping started');
    this.emit('status', { mapping: true, exploring: this.isExploring });
  }

  stopMapping() {
    this.isMapping = false;
    this.stopExploring();
    console.log('Mapper: mapping stopped');
    this.emit('status', { mapping: false, exploring: false });
  }

  resetMap() {
    this.grid.fill(UNKNOWN);
    this.pose = { x: 0, y: 0, theta: 0 };
    this.path = [{ x: 0, y: 0 }];
    this.lidarScan = [];
    this.obstacles = { front: 8000, back: 8000, left: 8000, right: 8000 };
    this.stats = { scans: 0, cellsExplored: 0, cellsOccupied: 0, distanceTravelled: 0 };
    this.emit('status', { mapping: this.isMapping, exploring: this.isExploring });
  }

  // --- Autonomous exploration with real obstacle avoidance ---
  startExploring() {
    if (!this.sendCommand) {
      console.log('Mapper: no sendCommand callback set');
      return;
    }
    if (!this.isMapping) this.startMapping();
    this.isExploring = true;
    this._exploreState = { stuckCount: 0, lastTurnDir: 1, sweepCount: 0 };
    console.log('Mapper: autonomous exploration started');
    this.emit('status', { mapping: true, exploring: true });
    // Delay first step to allow LiDAR scans to populate obstacle distances
    this.explorerInterval = setTimeout(() => this._exploreStep(), 2000);
  }

  stopExploring() {
    this.isExploring = false;
    if (this.explorerInterval) {
      clearTimeout(this.explorerInterval);
      this.explorerInterval = null;
    }
    if (this.sendCommand) {
      this.sendCommand('manual_move 0 0');
    }
    this.emit('status', { mapping: this.isMapping, exploring: false });
  }

  async _exploreStep() {
    if (!this.isExploring || !this.sendCommand) return;

    const obs = this.obstacles;
    const st = this._exploreState;
    let action = '';
    let waitMs = 1800;

    // Decision tree based on real obstacle distances:
    // 1. If front is clear (> SAFE_DISTANCE) → drive forward
    // 2. If front is blocked → find the clearest direction and turn toward it
    // 3. If stuck on all sides → do a 180 and backtrack
    // 4. Periodically seek frontiers (unexplored boundaries)

    const frontClear = obs.front > SAFE_DISTANCE_MM;
    const frontWarn = obs.front > WARNING_DISTANCE_MM;
    const leftClear = obs.left > SAFE_DISTANCE_MM;
    const rightClear = obs.right > SAFE_DISTANCE_MM;
    const backClear = obs.back > SAFE_DISTANCE_MM;

    // Every 8 steps, try to navigate toward nearest frontier
    st.sweepCount = (st.sweepCount || 0) + 1;
    if (st.sweepCount >= 8 && frontClear) {
      const frontier = this._findBestFrontier();
      if (frontier) {
        // Compute bearing to frontier
        const dx = frontier.wx - this.pose.x;
        const dy = frontier.wy - this.pose.y;
        const targetAngle = Math.atan2(dy, dx);
        let turnNeeded = ((targetAngle - this.pose.theta) * 180) / Math.PI;
        // Normalize to [-180, 180]
        while (turnNeeded > 180) turnNeeded -= 360;
        while (turnNeeded < -180) turnNeeded += 360;

        if (Math.abs(turnNeeded) > 15) {
          // Turn toward frontier
          const clampedTurn = Math.max(-90, Math.min(90, turnNeeded));
          action = `pre_rot ${Math.round(clampedTurn)} ${EXPLORE_SPEED}`;
          this.updatePoseRotation(clampedTurn);
          waitMs = 1500;
          st.sweepCount = 0;
          console.log(`Mapper explore: turning ${Math.round(clampedTurn)}° toward frontier`);
        } else {
          st.sweepCount = 0; // Already facing frontier, keep driving
        }
      }
    }

    if (!action) {
      if (frontClear) {
        // Path ahead is clear — drive forward
        const step = frontWarn ? EXPLORE_STEP_MM : Math.round(EXPLORE_STEP_MM * 0.6);
        action = `pre_drive ${step} ${EXPLORE_SPEED}`;
        this.updatePoseForward(step);
        st.stuckCount = 0;
        waitMs = 1800;
      } else if (leftClear && rightClear) {
        // Front blocked, both sides open — turn toward the more open side
        const turnDir = obs.left > obs.right ? -1 : 1; // -1 = left (CCW in LiDAR frame)
        const turnAngle = 45 * turnDir;
        action = `pre_rot ${turnAngle} ${EXPLORE_SPEED}`;
        this.updatePoseRotation(turnAngle);
        st.lastTurnDir = turnDir;
        waitMs = 1500;
        console.log(`Mapper explore: front blocked (${obs.front}mm), turning ${turnAngle}°`);
      } else if (leftClear) {
        // Turn left (counter-clockwise from above)
        action = `pre_rot -60 ${EXPLORE_SPEED}`;
        this.updatePoseRotation(-60);
        st.lastTurnDir = -1;
        waitMs = 1500;
        console.log(`Mapper explore: turning left, front=${obs.front}mm right=${obs.right}mm`);
      } else if (rightClear) {
        // Turn right
        action = `pre_rot 60 ${EXPLORE_SPEED}`;
        this.updatePoseRotation(60);
        st.lastTurnDir = 1;
        waitMs = 1500;
        console.log(`Mapper explore: turning right, front=${obs.front}mm left=${obs.left}mm`);
      } else if (backClear) {
        // Surrounded on three sides — back up and turn
        action = `pre_drive -150 ${EXPLORE_SPEED}`;
        this.updatePoseForward(-150);
        st.stuckCount++;
        waitMs = 2000;
        console.log(`Mapper explore: 3-side block, backing up (F:${obs.front} B:${obs.back} L:${obs.left} R:${obs.right})`);
      } else {
        // Very tight — find the most open direction and turn toward it
        st.stuckCount++;
        if (st.stuckCount > 10) {
          console.log('Mapper explore: stuck after 10 attempts, stopping');
          this.stopExploring();
          return;
        }
        // Turn toward whichever direction has the most space
        const best = Math.max(obs.front, obs.back, obs.left, obs.right);
        let turnAngle;
        if (best === obs.back) turnAngle = 180;
        else if (best === obs.left) turnAngle = -90;
        else if (best === obs.right) turnAngle = 90;
        else turnAngle = 30 * st.lastTurnDir; // front is somehow best, just nudge
        action = `pre_rot ${turnAngle} ${EXPLORE_SPEED}`;
        this.updatePoseRotation(turnAngle);
        waitMs = 1800;
        console.log(`Mapper explore: tight space, turning ${turnAngle}° (F:${obs.front} B:${obs.back} L:${obs.left} R:${obs.right})`);
      }
    }

    if (action) {
      try {
        await this.sendCommand(action);
      } catch (e) {
        console.log('Mapper explore command error:', e.message);
      }
    }

    this.explorerInterval = setTimeout(() => this._exploreStep(), waitMs);
  }

  _findBestFrontier() {
    const { gx: rgx, gy: rgy } = this.worldToGrid(this.pose.x, this.pose.y);
    let bestDist = Infinity;
    let bestFrontier = null;

    // Sample the grid at intervals for performance
    const step = 4;
    for (let gy = 0; gy < MAP_SIZE_CELLS; gy += step) {
      for (let gx = 0; gx < MAP_SIZE_CELLS; gx += step) {
        if (this.getCell(gx, gy) >= FREE_THRESHOLD) continue; // Not free

        // Check if this free cell borders unknown space
        const isUnknown = (v) => v >= FREE_THRESHOLD && v <= OCCUPIED_THRESHOLD;
        const hasUnknownNeighbor =
          isUnknown(this.getCell(gx - 1, gy)) ||
          isUnknown(this.getCell(gx + 1, gy)) ||
          isUnknown(this.getCell(gx, gy - 1)) ||
          isUnknown(this.getCell(gx, gy + 1));

        if (!hasUnknownNeighbor) continue;

        const dist = Math.abs(gx - rgx) + Math.abs(gy - rgy);
        // Prefer frontiers that are not too close (already scanning) but not too far
        if (dist > 5 && dist < bestDist) {
          bestDist = dist;
          bestFrontier = this.gridToWorld(gx, gy);
        }
      }
    }
    return bestFrontier;
  }

  _sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
  }

  // --- Get map state for API/UI ---
  getMapState() {
    // Threshold the log-odds grid for display
    const walls = [];
    const free = [];

    for (let gy = 0; gy < MAP_SIZE_CELLS; gy++) {
      for (let gx = 0; gx < MAP_SIZE_CELLS; gx++) {
        const v = this.getCell(gx, gy);
        if (v > OCCUPIED_THRESHOLD) walls.push(gx, gy);
        else if (v < FREE_THRESHOLD) free.push(gx, gy);
      }
    }

    return {
      walls,
      free,
      gridSize: MAP_SIZE_CELLS,
      cellSize: CELL_SIZE_MM,
      pose: this.pose,
      path: this.path.length > 500 ? this.path.filter((_, i) => i % 3 === 0 || i === this.path.length - 1) : this.path,
      obstacles: this.obstacles,
      stats: this.stats,
      isMapping: this.isMapping,
      isExploring: this.isExploring,
      lidarScan: this.lidarScan.length > 200
        ? this.lidarScan.filter((_, i) => i % 2 === 0)
        : this.lidarScan,
    };
  }
}

module.exports = { OhmniMapper, CELL_SIZE_MM, MAP_SIZE_CELLS, MAP_ORIGIN };
