/* ======== Lidar feature commands (dimos-friendly patch) ========
 *
 * Reproduces the stock bot_shell_lidar.js commands AND adds the
 * `lidar_get_scan` / `lidar_get_obstacles` commands needed by the
 * dimos-ohmni adapter. Direct-device commands (lidar_set_pwm,
 * lidar_scan, lidar_stop, lidar_release) resolve through
 *   this._botnode._lidarNode.collisionDetectionNode._lidarDevice
 * because `this._botnode._lidarDevice` is null on this firmware.
 *
 * Public commands:
 *   scan_lidar_device                -> populate _lidarDevice
 *   start_collision_detection        -> create+start CD node
 *   stop_collision_detection
 *   toggle_collision_detection on|off
 *   update_lidar_config <isFront 0|1> <fw> <fs> <bw> <bs> <speed>
 *   lidar_set_pwm <0-1023>
 *   lidar_scan
 *   lidar_stop
 *   lidar_release
 *   lidar_get_obstacles              -> "OBS:..."
 *   lidar_get_scan                   -> "SCAN:angle:dist,...|OBS:..."
 */

module.exports = function(BotShell) {

  // -- helpers -------------------------------------------------------

  function getCD(self) {
    var lidarNode = self._botnode && self._botnode._lidarNode;
    return lidarNode && lidarNode.collisionDetectionNode;
  }

  function getDevice(self) {
    var cd = getCD(self);
    return cd && cd._lidarDevice;
  }

  // -- stock commands restored --------------------------------------

  BotShell.prototype.cmd_scan_lidar_device = function(parts) {
    if (parts.length !== 0) { this.log("Usage: scan_lidar_device"); return; }
    this._botnode.scanLidarDevice();
    this.log("scan lidar device");
  };

  BotShell.prototype.cmd_start_collision_detection = function(parts) {
    if (parts.length !== 0) { this.log("Usage: start_collision_detection"); return; }
    // Lazily populate _lidarDevice if scan_lidar_device wasn't called first.
    if (!this._botnode._lidarDevice) {
      try { this._botnode.scanLidarDevice(); } catch (e) {}
    }
    this._botnode.startCollisionDetection();
    this.log("start collion detection node");
  };

  BotShell.prototype.cmd_stop_collision_detection = function(parts) {
    if (parts.length !== 0) { this.log("Usage: stop_collision_detection"); return; }
    this._botnode.stopCollisionDetection();
    this.log("stop collion detection node");
  };

  BotShell.prototype.cmd_toggle_collision_detection = function(parts) {
    if (parts.length < 1) { this.log("Usage: toggle_collision_detection <on|off>"); return; }
    var enable = parts[0] === 'on';
    this.log(enable ? 'start obstacle detection' : 'stop obstacle detection');
    if (this._botnode._lidarNode) {
      this._botnode._lidarNode.toggleCollisionDetection(enable);
    }
  };

  BotShell.prototype.cmd_update_lidar_config = function(parts) {
    if (parts.length !== 6) {
      this.log("Usage: update_lidar_config <isInFront: 0|1> <front_warning> <front_stopping> <back_warning> <back_stopping> <speed>");
      return;
    }
    var isInFront = parts[0] === '1';
    var nums = parts.slice(1).map(function(v) { return parseInt(v); });
    if (nums.some(function(v) { return !v; })) {
      this.log("collision detection config is invalid"); return;
    }
    this._botnode.cmd_configCollisionDetection({
      isInFront: isInFront,
      frontWarningDistance: nums[0],
      frontStoppingDistance: nums[1],
      backWarningDistance: nums[2],
      backStoppingDistance: nums[3],
      lidarRotationSpeed: nums[4],
    });
  };

  // -- direct-device commands (re-routed to working device) ---------

  BotShell.prototype.cmd_lidar_stop = function(parts) {
    if (parts.length !== 0) { this.log("Usage: lidar_stop"); return; }
    var dev = getDevice(this);
    if (!dev) { this.log("There is no lidar device"); return; }
    try { dev.stop_scan(); } catch (e) {}
    try { dev.set_motor_pwm(0); } catch (e) {}
    this.log("stop lidar");
  };

  BotShell.prototype.cmd_lidar_set_pwm = function(parts) {
    if (parts.length < 1) { this.log("Usage: lidar_set_pwm <value 0-1023>"); return; }
    var dev = getDevice(this);
    if (!dev) { this.log("There is no lidar device"); return; }
    var pwm = parseInt(parts[0]);
    if (pwm) try { dev.set_motor_pwm(pwm); } catch (e) {}
    this.log("set lidar pwm");
  };

  BotShell.prototype.cmd_lidar_scan = function(parts) {
    if (parts.length !== 0) { this.log("Usage: cmd_lidar_scan"); return; }
    var dev = getDevice(this);
    if (!dev) { this.log("There is no lidar device"); return; }
    try { dev.start_express_scan(); } catch (e) {}
    this.log("Scan lidar");
  };

  BotShell.prototype.cmd_lidar_release = function(parts) {
    if (parts.length !== 0) { this.log("Usage: lidar_release"); return; }
    if (this._botnode && typeof this._botnode.releaseLidar === 'function') {
      this._botnode.releaseLidar();
      this.log("Release lidar device");
    } else {
      this.log("releaseLidar unavailable");
    }
  };

  // -- new commands for off-board mapping/exploration ---------------

  BotShell.prototype.cmd_lidar_get_obstacles = function(parts) {
    var cd = getCD(this);
    if (!cd) { this.log("OBS_ERR:no_collision_detection"); return; }
    this.log("OBS:" + ((cd.obstacleMap || []).join(",")));
  };

  // Diagnostic: report what state every lidar object is in.
  BotShell.prototype.cmd_lidar_status = function(parts) {
    var bn = this._botnode;
    var ln = bn && bn._lidarNode;
    var cd = ln && ln.collisionDetectionNode;
    var devTop = bn && bn._lidarDevice;
    var devCD = cd && cd._lidarDevice;
    var lines = [];
    lines.push("STATUS:");
    lines.push("  botnode=" + (bn ? "yes" : "NO"));
    lines.push("  lidarNode=" + (ln ? "yes" : "NO"));
    lines.push("  collisionDetectionNode=" + (cd ? "yes" : "NO"));
    lines.push("  botnode._lidarDevice=" + (devTop ? ("yes opened=" + (devTop._opened ? 1 : 0) + " port=" + devTop._portstr) : "NO"));
    lines.push("  cd._lidarDevice=" + (devCD ? ("yes opened=" + (devCD._opened ? 1 : 0) + " port=" + devCD._portstr) : "NO"));
    if (cd) {
      lines.push("  cd.isReady=" + (typeof cd.isReady !== "undefined" ? cd.isReady : "?"));
      lines.push("  cd.pwm=" + (typeof cd.pwm !== "undefined" ? cd.pwm : "?"));
      lines.push("  cd.obstacleMap=" + JSON.stringify(cd.obstacleMap || null));
      // Any cabin listeners attached?
      if (devCD && typeof devCD.listenerCount === "function") {
        lines.push("  new_cabin_listeners=" + devCD.listenerCount("new_cabin"));
      }
    }
    if (devCD) {
      lines.push("  cd._lidarDevice keys=" + Object.keys(devCD).slice(0,20).join(","));
    }
    var self = this;
    lines.forEach(function(l) { self.log(l); });
  };

  // ---- Independent express-scan parser ----
  //
  // We tap the underlying SerialPort, accumulate raw bytes, and decode
  // express-scan cabin packets ourselves. This is robust against the
  // on-device LidarSerial state machine getting wedged waiting for a
  // descriptor that already passed.
  //
  // Express scan packet layout (84 bytes):
  //   byte 0: sync1 (0xA in high nibble) | checksum_low_nibble
  //   byte 1: sync2 (0x5 in high nibble) | checksum_high_nibble
  //   byte 2-3: start_angle_q6 (low byte | high byte's 7 LSB)
  //   byte 4..83: 16 cabins, 5 bytes each
  //
  // We don't bother verifying checksum — the parser just looks for the
  // sync, decodes 16 cabins, and emits {angle, distance} pairs.

  function _decodeExpressPackets(buffer) {
    // Find the first sync, scan packets from there.
    var readings = [];
    var prevStart = null;
    var prevPacket = null;
    var i = 0;
    while (i + 84 <= buffer.length) {
      var b0 = buffer[i];
      var b1 = buffer[i + 1];
      if (((b0 & 0xF0) >> 4) !== 0xA || ((b1 & 0xF0) >> 4) !== 0x5) {
        i++;
        continue;
      }
      var startQ6 = buffer[i + 2] | ((buffer[i + 3] & 0x7F) << 8);
      var start = startQ6 / 64.0;
      if (prevPacket !== null) {
        var diff = start - prevStart;
        if (diff < 0) diff += 360;
        var angInc = diff / 32.0;  // 16 cabins * 2 readings each
        for (var c = 0; c < 16; c++) {
          var off = i + 4 + c * 5;
          // (Use prevPacket's payload for cabin distances)
          var off2 = 4 + c * 5;
          var d1 = (prevPacket[off2] >> 2) | (prevPacket[off2 + 1] << 6);
          var d2 = (prevPacket[off2 + 2] >> 2) | (prevPacket[off2 + 3] << 6);
          var ti1 = ((prevPacket[off2 + 4] & 0x0F) << 4) | (prevPacket[off2] & 0x03);
          var ti2 = ((prevPacket[off2 + 4] & 0xF0)) | (prevPacket[off2 + 2] & 0x03);
          var theta1 = (ti1 & 0x20) ? (-(ti1 & 0x1F) / 8.0) : (ti1 / 8.0);
          var theta2 = (ti2 & 0x20) ? (-(ti2 & 0x1F) / 8.0) : (ti2 / 8.0);
          var a1 = (prevStart + angInc * (c * 2)) - theta1;
          var a2 = (prevStart + angInc * (c * 2 + 1)) - theta2;
          a1 = ((a1 % 360) + 360) % 360;
          a2 = ((a2 % 360) + 360) % 360;
          if (d1 > 0) readings.push(Math.round(a1 * 10) / 10 + ":" + d1);
          if (d2 > 0) readings.push(Math.round(a2 * 10) / 10 + ":" + d2);
        }
      }
      prevStart = start;
      prevPacket = buffer.slice(i, i + 84);
      i += 84;
    }
    return readings;
  }

  BotShell.prototype.cmd_lidar_get_scan_v2 = function(parts) {
    var self = this;
    var cd = getCD(this);
    var dev = cd && cd._lidarDevice;
    if (!dev || !dev._sport) { self.log("SCAN_ERR:no_device"); return; }
    var sport = dev._sport;
    var chunks = [];
    var listener = function(data) { chunks.push(data); };
    sport.on("data", listener);
    setTimeout(function() {
      try { sport.removeListener("data", listener); } catch (e) {}
      var total = Buffer.concat(chunks);
      var readings = _decodeExpressPackets(total);
      var obs = (cd && cd.obstacleMap) ? cd.obstacleMap.join(",") : "0,0,0";
      if (readings.length > 0) {
        self.log("SCAN:" + readings.join(",") + "|OBS:" + obs);
      } else {
        self.log("SCAN_EMPTY|raw=" + total.length + "|OBS:" + obs);
      }
    }, 200);
  };

  // Tap the underlying SerialPort for 500ms and report how many raw
  // bytes arrive. If 0, the lidar physically isn't sending data.
  // If >0 but no `new_cabin` events fire, the parser is the problem.
  BotShell.prototype.cmd_lidar_tap = function(parts) {
    var self = this;
    var cd = getCD(this);
    var dev = cd && cd._lidarDevice;
    if (!dev || !dev._sport) { self.log("TAP_ERR:no_device"); return; }
    var sport = dev._sport;
    var bytesIn = 0;
    var firstBytes = [];
    var listener = function(data) {
      bytesIn += data.length;
      if (firstBytes.length < 32) {
        for (var i = 0; i < data.length && firstBytes.length < 32; i++) {
          firstBytes.push(data[i].toString(16).padStart(2, "0"));
        }
      }
    };
    sport.on("data", listener);
    setTimeout(function() {
      try { sport.removeListener("data", listener); } catch (e) {}
      self.log("TAP:bytes=" + bytesIn + " first=" + firstBytes.join(""));
    }, 500);
  };

  // Force-restart the lidar serial device. Useful when the existing
  // _lidarDevice is open but no `new_cabin` events flow.
  BotShell.prototype.cmd_lidar_restart = function(parts) {
    if (parts.length !== 0) { this.log("Usage: lidar_restart"); return; }
    var bn = this._botnode;
    if (!bn) { this.log("RESTART_ERR:no_botnode"); return; }
    try {
      if (bn._lidarNode && bn._lidarNode.isCollisionDetectionAvailable()) {
        bn._lidarNode.stopCollisionDetection();
      }
    } catch (e) {}
    try {
      if (typeof bn.releaseLidar === "function") bn.releaseLidar();
    } catch (e) {}
    try {
      bn.scanLidarDevice();
    } catch (e) {}
    var self = this;
    setTimeout(function() {
      try { bn.startCollisionDetection(); } catch (e) {}
      self.log("lidar_restart done");
    }, 1000);
  };

  // Collect cabin events for ~one revolution (~91ms at 660 RPM, allow 150ms)
  // and return them as: SCAN:a1:d1,a2:d2,...|OBS:zone0,zone1,...
  BotShell.prototype.cmd_lidar_get_scan = function(parts) {
    var self = this;
    var cd = getCD(this);
    var dev = getDevice(this);
    if (!cd) { self.log("SCAN_ERR:no_collision_detection"); return; }
    if (!dev || !dev._opened) { self.log("SCAN_ERR:lidar_not_open"); return; }

    var readings = [];
    var handler = function(cabin) {
      if (cabin.distance > 0 && cabin.angle >= 0 && cabin.angle <= 360) {
        readings.push(
          (Math.round(cabin.angle * 10) / 10) + ":" + Math.round(cabin.distance)
        );
      }
    };
    dev.on("new_cabin", handler);

    setTimeout(function() {
      try { dev.removeListener("new_cabin", handler); } catch (e) {}
      var obs = (cd.obstacleMap || []).join(",");
      if (readings.length > 0) {
        self.log("SCAN:" + readings.join(",") + "|OBS:" + obs);
      } else {
        self.log("SCAN_EMPTY|OBS:" + obs);
      }
    }, 150);
  };
};
