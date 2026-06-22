
/* ======== Lidar feature tests ======== */
module.exports = function(BotShell) {
  // Manually stop the Lidar motor
  BotShell.prototype.cmd_lidar_stop = function(parts) {
    // Parsing inputs
    if (parts.length !== 0) {
      this.log("Usage: lidar_stop");
      return;
    }
    if (!this._botnode._lidarDevice) {
      this.log("There is no lidar device")
      return;
    };
    this._botnode._lidarDevice.stop_scan();
    this._botnode._lidarDevice.set_motor_pwm(0);
    this.log("stop lidar");
  }

  // Manually set Lidar motor PWM
  BotShell.prototype.cmd_lidar_set_pwm = function(parts) {
    // Parsing inputs
    if (parts.length < 1) {
      this.log("Usage: lidar_set_pwm <value 0-1023>");
      return;
    }

    if (!this._botnode._lidarDevice) {
      this.log("There is no lidar device")
      return;
    };

    const pwm = parseInt(parts[0]);

    // Send out to lidar
    if (pwm) this._botnode._lidarDevice.set_motor_pwm(pwm);
    this.log("set lidar pwm");
  }

  // Start express scan
  BotShell.prototype.cmd_lidar_scan = function(parts) {
    // Parsing inputs
    if (parts.length !== 0) {
      this.log("Usage: cmd_lidar_scan");
      return;
    }

    if (!this._botnode._lidarDevice) {
      this.log("There is no lidar device")
      return;
    };

    this._botnode._lidarDevice.start_express_scan();
    this.log("Scan lidar");
  }

  // Release lidar device
  BotShell.prototype.cmd_lidar_release = function(parts) {
    // Parsing inputs
    if (parts.length !== 0) {
      this.log("Usage: lidar_release");
      return;
    }
    this._botnode.releaseLidar();
    this.log("Release lidar device");
  }

  // Scan lidar device in the bot
  BotShell.prototype.cmd_scan_lidar_device = function(parts) {
    // Parsing inputs
    if (parts.length !== 0) {
      this.log("Usage: scan_lidar_device");
      return;
    }
    this._botnode.scanLidarDevice();
    this.log("scan lidar device");
  }

  // turn on | of obstacle avoidance
  BotShell.prototype.cmd_toggle_collision_detection = function(parts) {
    // Parsing inputs
    if (parts.length < 1) {
      this.log("Usage: toggle_collision_detection <'on'| 'off'>");
      return;
    }
    const enable = parts[0] === 'on';
    this.log(enable ? 'start obstacle detection' : 'stop obstacle detection');
    this._botnode._lidarNode.toggleCollisionDetection(enable);
  }

  // start obstacle avoidance node
  BotShell.prototype.cmd_start_collision_detection = function(parts) {
    // Parsing inputs
    if (parts.length !== 0) {
      this.log("Usage: start_collision_detection");
      return;
    }
    this._botnode.startCollisionDetection();
    this.log("start collion detection node");
  }

  // stop obstacle avoidance node
  BotShell.prototype.cmd_stop_collision_detection = function(parts) {
    // Parsing inputs
    if (parts.length !== 0) {
      this.log("Usage: stop_collision_detection");
      return;
    }
    this._botnode.stopCollisionDetection();
    this.log("stop collion detection node");
  }

  // update the lidar config
  BotShell.prototype.cmd_update_lidar_config = function (parts) {
    // Parsing inputs
    if (parts.length !== 6) {
      this.log("Usage: update_lidar_config <isInFront: 0|1> <front_warning> <front_stopping> <back_warning> <back_stopping> <speed>");
      return;
    }

    const isInFront = parts[0] === '1';
    parts.splice(0, 1);
    const formatedParts = parts.map(value => parseInt(value));
    const isInvalid = formatedParts.some(value => !value);
    if (isInvalid) {
      this.log("collision detection config is invalid");
      return;
    }
    const [
      frontWarningDistance,
      frontStoppingDistance,
      backWarningDistance,
      backStoppingDistance,
      lidarRotationSpeed,
    ] = formatedParts;
    this._botnode.cmd_configCollisionDetection({
      isInFront,
      frontWarningDistance,
      frontStoppingDistance,
      backWarningDistance,
      backStoppingDistance,
      lidarRotationSpeed
    });
  }

  // === NEW: Get raw LIDAR scan data for mapping ===
  // Collects angle/distance pairs from one revolution (~100ms at 660 RPM)
  // and returns them as a parseable string: "SCAN:angle:dist,angle:dist,..."
  BotShell.prototype.cmd_lidar_get_scan = function(parts) {
    var self = this;
    var lidarNode = this._botnode._lidarNode;
    if (!lidarNode || !lidarNode.collisionDetectionNode) {
      this.log("SCAN_ERR:no_collision_detection");
      return;
    }

    var cd = lidarNode.collisionDetectionNode;
    var lidarDevice = cd._lidarDevice;
    if (!lidarDevice || !lidarDevice._opened) {
      this.log("SCAN_ERR:lidar_not_open");
      return;
    }

    // Collect cabin data for one revolution
    var readings = [];
    var handler = function(cabin) {
      if (cabin.distance > 0 && cabin.angle >= 0 && cabin.angle <= 360) {
        readings.push(Math.round(cabin.angle * 10) / 10 + ":" + Math.round(cabin.distance));
      }
    };

    lidarDevice.on("new_cabin", handler);

    // Collect for 150ms (one revolution at 660 RPM ~ 91ms)
    setTimeout(function() {
      lidarDevice.removeListener("new_cabin", handler);

      // Also include obstacle zone states
      var obs = cd.obstacleMap;
      var obsStr = "OBS:" + obs.join(",");

      if (readings.length > 0) {
        self.log("SCAN:" + readings.join(",") + "|" + obsStr);
      } else {
        self.log("SCAN_EMPTY|" + obsStr);
      }
    }, 150);
  }

  // === NEW: Get obstacle distances (lighter than full scan) ===
  // Returns the minimum distance per zone from current revolution data
  BotShell.prototype.cmd_lidar_get_obstacles = function(parts) {
    var lidarNode = this._botnode._lidarNode;
    if (!lidarNode || !lidarNode.collisionDetectionNode) {
      this.log("OBS_ERR:no_collision_detection");
      return;
    }

    var cd = lidarNode.collisionDetectionNode;
    var revData = cd._revData;
    var zones = cd._detLidar;

    // Calculate min distance per zone from current revolution data
    var results = [];
    for (var i = 0; i < zones.length; i++) {
      var zone = zones[i];
      var minDist = 8000;
      var angleInterval = cd._angleInterval;
      var start = zone.minTheta >> angleInterval;
      var end = zone.maxTheta >> angleInterval;

      if (start > end) {
        // Wrapping zone (e.g., 330-30)
        for (var k in revData) {
          var ka = parseInt(k);
          if (ka >= start || ka <= end) {
            if (revData[k] < minDist) minDist = revData[k];
          }
        }
      } else {
        for (var k in revData) {
          var ka = parseInt(k);
          if (ka >= start && ka <= end) {
            if (revData[k] < minDist) minDist = revData[k];
          }
        }
      }
      results.push(zone.direction + ":" + Math.round(minDist));
    }

    this.log("OBSTACLES:" + results.join(",") + "|STATE:" + cd.obstacleMap.join(","));
  }
}
