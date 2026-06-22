
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
}