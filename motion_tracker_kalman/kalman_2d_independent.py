#!/usr/bin/env python3
"""
Independent 2D Kalman Filter Motion Tracker
Two-stage sensor fusion: IMU orientation → world-frame motion tracking

ARCHITECTURE:
  Stage 1: IMU Kalman - Fuses gyro + accel + mag → device orientation (roll, pitch, yaw)
  Stage 2: Motion Kalman - Uses orientation to remove gravity and transform to world frame
           Then fuses GPS + world-frame accel for smooth 2D position tracking

SENSORS USED:
  - Gyroscope: Angular velocity (50 Hz) - primary for orientation integration
  - Accelerometer: Raw 3-axis accel (50 Hz) - gravity vector reveals orientation
  - Magnetometer: Magnetic field vector - heading reference (yaw)
  - GPS: World position (1 Hz) - ground truth for position

GRAVITY HANDLING:
  - Gravity is REMOVED using device orientation from IMU Kalman
  - Device orientation = roll (rotation around X) + pitch (rotation around Y) + yaw (rotation around Z)
  - Gravity always points down: [0, 0, -9.81] in world frame
  - In device frame: depends on how device is tilted

HEADING:
  - Magnetometer provides magnetic field direction (compass)
  - Used for yaw angle (world heading) - tells us which way device points
  - Gyro integrates to track yaw over time
  - Kalman fuses them to filter magnetic interference
"""

import numpy as np
import math
import threading
import time
from collections import deque
from typing import Tuple, Dict, Optional


# ============================================================================
# STAGE 1: IMU KALMAN FILTER (Orientation Estimation)
# ============================================================================

class IMUKalmanFilter:
    """
    Estimates device orientation (roll, pitch, yaw) from IMU sensors.

    State vector (3D):
      [roll, pitch, yaw]  = device orientation in Euler angles (radians)

    Measurements:
      From accelerometer: gravity vector → roll and pitch
      From magnetometer: magnetic field → yaw
      From gyroscope: angular velocity (integrated implicitly in prediction)

    Fusion approach:
      - Gyro predicts: simple integration of angular rates
      - Accel+Mag correct: measure absolute orientation, fuse with gyro estimate
    """

    def __init__(self, dt: float = 0.02):
        """
        Args:
            dt: Time step (seconds) - should match IMU sampling period
        """
        self.dt = dt
        self.g = 9.81  # Gravity magnitude

        # State: [roll, pitch, yaw]
        self.state = np.array([0.0, 0.0, 0.0])  # radians

        # State covariance (uncertainty in each angle estimate)
        self.P = np.eye(3) * 0.1  # Start with high uncertainty

        # Process noise (gyroscope drift, integration error)
        # Larger = trust gyro less, accel/mag more
        self.Q = np.eye(3) * 0.001  # Process noise covariance

        # Measurement noise (sensor reliability)
        # Accel noise: typically 0.01-0.1 m/s²
        # Mag noise: varies by environment (less in shielded areas)
        self.R_accel_mag = np.eye(2) * 0.05  # For roll, pitch from accel
        self.R_yaw = 0.1  # For yaw from mag

        # Last gyro readings (for integration)
        self.last_gyro = np.array([0.0, 0.0, 0.0])

        # Thread safety
        self.lock = threading.Lock()

    def normalize_angle(self, angle: float) -> float:
        """Wrap angle to [-π, π]"""
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    def accel_to_roll_pitch(self, accel: np.ndarray) -> Tuple[float, float]:
        """
        Extract roll and pitch from accelerometer readings.

        Principle: Gravity vector reveals device tilt
          - If Z-axis reads -9.81: device level (roll=0, pitch=0)
          - If X-axis reads -9.81: device rotated 90° around Y (pitch=90°)
          - If Y-axis reads -9.81: device rotated 90° around X (roll=90°)

        Args:
            accel: [ax, ay, az] in m/s² (includes gravity)

        Returns:
            (roll, pitch) in radians
        """
        ax, ay, az = accel[0], accel[1], accel[2]

        # Avoid division by zero
        if abs(az) < 0.01:
            az = 0.01 if az >= 0 else -0.01

        # Roll: rotation around X-axis (affects Y and Z)
        # tan(roll) = ay / az
        roll = math.atan2(ay, az)

        # Pitch: rotation around Y-axis (affects X and Z)
        # tan(pitch) = -ax / sqrt(ay² + az²)
        pitch = math.atan2(-ax, math.sqrt(ay**2 + az**2))

        return roll, pitch

    def mag_to_yaw(self, mag: np.ndarray, roll: float, pitch: float) -> float:
        """
        Extract yaw from magnetometer readings, corrected for roll/pitch tilt.

        Principle: Magnetometer points to magnetic north in world frame.
        We need to:
          1. Rotate mag vector back to world frame (undo roll and pitch)
          2. Extract heading from world-frame magnetic field

        Args:
            mag: [mx, my, mz] raw magnetometer readings
            roll, pitch: Device orientation angles

        Returns:
            yaw in radians (0 = magnetic north, π/2 = east, etc.)
        """
        mx, my, mz = mag[0], mag[1], mag[2]

        # Tilt-compensated heading calculation
        # First, remove pitch effect (rotation around Y)
        cos_p = math.cos(pitch)
        sin_p = math.sin(pitch)
        sin_r = math.sin(roll)
        cos_r = math.cos(roll)

        # Magnetometer in world frame (approximately, tilt-corrected)
        # Simplified: project to horizontal plane
        mag_x_h = mx * cos_p + mz * sin_p
        mag_y_h = my - sin_r * sin_p * mx + cos_r * sin_p * mz

        # Yaw = heading in horizontal plane
        yaw = math.atan2(-mag_y_h, mag_x_h)

        return yaw

    def predict(self, gyro: np.ndarray) -> None:
        """
        Prediction step: integrate gyroscope angular rates.

        Args:
            gyro: [gx, gy, gz] angular velocity in rad/s
        """
        with self.lock:
            # Simple Euler integration for angles
            # This is a simplification (proper way is quaternions)
            # but works well for small dt and moderate rotation rates
            self.state[0] += gyro[0] * self.dt  # roll += gx * dt
            self.state[1] += gyro[1] * self.dt  # pitch += gy * dt
            self.state[2] += gyro[2] * self.dt  # yaw += gz * dt

            # Wrap yaw to [-π, π]
            self.state[2] = self.normalize_angle(self.state[2])

            # Covariance prediction: P = P + Q (increase uncertainty over time)
            self.P += self.Q

            self.last_gyro = gyro.copy()

    def update(self, accel: np.ndarray, mag: np.ndarray) -> None:
        """
        Update step: correct gyro estimate using accelerometer and magnetometer.

        Args:
            accel: [ax, ay, az] in m/s²
            mag: [mx, my, mz] magnetometer readings
        """
        with self.lock:
            # Measurement from accelerometer: roll and pitch
            roll_meas, pitch_meas = self.accel_to_roll_pitch(accel)

            # Measurement from magnetometer: yaw
            yaw_meas = self.mag_to_yaw(mag, self.state[0], self.state[1])

            # Kalman update for roll and pitch (using accel)
            # Measurement residual (difference between measurement and prediction)
            y_roll = self.normalize_angle(roll_meas - self.state[0])
            y_pitch = self.normalize_angle(pitch_meas - self.state[1])

            # Kalman gain (how much to trust measurement vs prediction)
            # K = P * H^T / (H * P * H^T + R)
            # Simplified for diagonal case
            S_roll = self.P[0, 0] + self.R_accel_mag[0, 0]
            K_roll = self.P[0, 0] / S_roll

            S_pitch = self.P[1, 1] + self.R_accel_mag[1, 1]
            K_pitch = self.P[1, 1] / S_pitch

            # State update: x = x + K * y
            self.state[0] += K_roll * y_roll
            self.state[1] += K_pitch * y_pitch

            # Covariance update: P = (I - K*H) * P
            self.P[0, 0] *= (1 - K_roll)
            self.P[1, 1] *= (1 - K_pitch)

            # Update yaw separately with lower gain (magnetometer less reliable)
            y_yaw = self.normalize_angle(yaw_meas - self.state[2])
            S_yaw = self.P[2, 2] + self.R_yaw
            K_yaw = self.P[2, 2] / S_yaw
            self.state[2] += K_yaw * y_yaw
            self.P[2, 2] *= (1 - K_yaw)

    def get_orientation(self) -> Tuple[float, float, float]:
        """
        Get current orientation estimate.

        Returns:
            (roll, pitch, yaw) in radians
        """
        with self.lock:
            return tuple(self.state.copy())


# ============================================================================
# STAGE 2: MOTION KALMAN FILTER (2D Position Tracking)
# ============================================================================

class MotionKalmanFilter:
    """
    Estimates 2D position and velocity in world frame.

    State vector (6D):
      [x, y, vel_x, vel_y, acc_x, acc_y]
      - x, y: position in meters (world frame)
      - vel_x, vel_y: velocity in m/s (world frame)
      - acc_x, acc_y: acceleration in m/s² (world frame)

    Measurements:
      - GPS: [x, y] (1 Hz, high latency but accurate)
      - Accelerometer: [ax, ay] in world frame (50 Hz, noisy but high frequency)

    Sensor fusion principle:
      - GPS provides ground truth position (but slow)
      - Accelerometer provides high-frequency updates (but drifts without GPS correction)
      - Kalman smoothly blends them based on covariance
    """

    def __init__(self, dt: float = 0.02, gps_noise_std: float = 5.0,
                 accel_noise_std: float = 0.3):
        """
        Args:
            dt: Time step for accelerometer updates (seconds)
            gps_noise_std: GPS position standard deviation (meters) - typical 5-10m
            accel_noise_std: Accel measurement noise (m/s²) - typical 0.1-0.5
        """
        self.dt = dt
        self.g = 9.81

        # State: [x, y, vel_x, vel_y, acc_x, acc_y]
        self.state = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

        # State transition matrix (constant acceleration model)
        # x(t+dt) = x(t) + vel*dt + 0.5*acc*dt²
        # vel(t+dt) = vel(t) + acc*dt
        # acc(t+dt) = acc(t) (assume constant over short time)
        self.F = np.array([
            [1, 0, self.dt, 0, 0.5*self.dt**2, 0],
            [0, 1, 0, self.dt, 0, 0.5*self.dt**2],
            [0, 0, 1, 0, self.dt, 0],
            [0, 0, 0, 1, 0, self.dt],
            [0, 0, 0, 0, 1, 0],
            [0, 0, 0, 0, 0, 1]
        ])

        # Measurement matrices
        # GPS measures position only
        self.H_gps = np.array([
            [1, 0, 0, 0, 0, 0],
            [0, 1, 0, 0, 0, 0]
        ])

        # Accelerometer measures acceleration only
        self.H_accel = np.array([
            [0, 0, 0, 0, 1, 0],
            [0, 0, 0, 0, 0, 1]
        ])

        # Measurement noise covariances
        self.R_gps = np.eye(2) * (gps_noise_std ** 2)
        self.R_accel = np.eye(2) * (accel_noise_std ** 2)

        # Process noise (uncertainty in acceleration between measurements)
        # Larger = less trust in constant acceleration assumption
        process_noise_accel = 0.1  # m/s²
        self.Q = np.eye(6) * 0.0001
        self.Q[4, 4] = process_noise_accel ** 2  # Accel noise
        self.Q[5, 5] = process_noise_accel ** 2

        # State covariance (uncertainty in state estimate)
        self.P = np.eye(6)
        self.P[0:2, 0:2] *= 100  # High uncertainty in position initially
        self.P[2:4, 2:4] *= 10   # Medium uncertainty in velocity
        self.P[4:6, 4:6] *= 1    # Moderate uncertainty in acceleration

        # Origin for GPS conversion
        self.origin_lat = None
        self.origin_lon = None
        self.last_gps_xy = None

        # Thread safety
        self.lock = threading.Lock()

    def lat_lon_to_meters(self, lat: float, lon: float) -> Tuple[float, float]:
        """
        Convert GPS lat/lon to local x/y meters from origin.
        Uses equirectangular projection (accurate for short distances).

        Args:
            lat, lon: GPS coordinates in degrees

        Returns:
            (x, y) in meters from origin
        """
        if self.origin_lat is None:
            self.origin_lat = lat
            self.origin_lon = lon
            return 0.0, 0.0

        R = 6371000  # Earth radius in meters

        lat_rad = math.radians(lat)
        origin_lat_rad = math.radians(self.origin_lat)

        x = R * math.radians(lon - self.origin_lon) * math.cos(origin_lat_rad)
        y = R * math.radians(lat - self.origin_lat)

        return x, y

    def predict(self) -> None:
        """
        Prediction step: integrate state using constant acceleration model.
        """
        with self.lock:
            # x(t+dt) = F * x(t)
            self.state = self.F @ self.state

            # P(t+dt) = F * P(t) * F^T + Q
            self.P = self.F @ self.P @ self.F.T + self.Q

    def update_gps(self, lat: float, lon: float) -> None:
        """
        Update with GPS measurement (position only).

        Args:
            lat, lon: GPS coordinates in degrees
        """
        with self.lock:
            x, y = self.lat_lon_to_meters(lat, lon)
            self.last_gps_xy = np.array([x, y])

            # Measurement vector: [x, y]
            z = np.array([[x], [y]])

            # Innovation (measurement residual)
            y_gps = z - self.H_gps @ self.state.reshape(-1, 1)

            # Innovation covariance: S = H*P*H^T + R
            S = self.H_gps @ self.P @ self.H_gps.T + self.R_gps

            # Kalman gain: K = P*H^T*S^-1
            try:
                K = self.P @ self.H_gps.T @ np.linalg.inv(S)
            except np.linalg.LinAlgError:
                # Singular matrix, skip update
                return

            # State update: x = x + K*y
            self.state = self.state.reshape(-1, 1) + K @ y_gps
            self.state = self.state.flatten()

            # Covariance update: P = (I - K*H)*P
            self.P = (np.eye(6) - K @ self.H_gps) @ self.P

    def update_accelerometer(self, accel_world: np.ndarray) -> None:
        """
        Update with accelerometer measurement in world frame.

        Args:
            accel_world: [ax, ay] in world frame (gravity already removed), m/s²
        """
        with self.lock:
            z = accel_world.reshape(-1, 1)

            # Innovation
            y_accel = z - self.H_accel @ self.state.reshape(-1, 1)

            # Innovation covariance
            S = self.H_accel @ self.P @ self.H_accel.T + self.R_accel

            # Kalman gain
            try:
                K = self.P @ self.H_accel.T @ np.linalg.inv(S)
            except np.linalg.LinAlgError:
                return

            # State update
            self.state = self.state.reshape(-1, 1) + K @ y_accel
            self.state = self.state.flatten()

            # Covariance update
            self.P = (np.eye(6) - K @ self.H_accel) @ self.P

    def get_state(self) -> Dict[str, float]:
        """
        Get current state estimate.

        Returns:
            Dict with keys: x, y, vel_x, vel_y, acc_x, acc_y
        """
        with self.lock:
            return {
                'x': self.state[0],
                'y': self.state[1],
                'vel_x': self.state[2],
                'vel_y': self.state[3],
                'acc_x': self.state[4],
                'acc_y': self.state[5]
            }


# ============================================================================
# INTEGRATED SENSOR FUSION SYSTEM
# ============================================================================

class TwoStageKalmanFusion:
    """
    Complete independent 2D Kalman filter system.

    WORKFLOW:
      1. Read raw sensors: gyro, accel, mag (50 Hz), GPS (1 Hz)
      2. Gyro + Accel + Mag → IMU Kalman → Device orientation
      3. Use orientation to remove gravity from accel
      4. Transform gravity-free accel to world frame using yaw
      5. World-frame accel + GPS → Motion Kalman → 2D position

    OUTPUT:
      - Device orientation (roll, pitch, yaw)
      - World-frame 2D position, velocity, acceleration
    """

    def __init__(self, accel_sample_rate: float = 50,
                 gps_noise_std: float = 5.0,
                 accel_noise_std: float = 0.3):
        """
        Args:
            accel_sample_rate: IMU sampling rate in Hz
            gps_noise_std: GPS position noise standard deviation (meters)
            accel_noise_std: Accelerometer noise standard deviation (m/s²)
        """
        self.dt = 1.0 / accel_sample_rate
        self.g = 9.81

        # Stage 1: IMU orientation
        self.imu_kalman = IMUKalmanFilter(dt=self.dt)

        # Stage 2: Motion tracking
        self.motion_kalman = MotionKalmanFilter(
            dt=self.dt,
            gps_noise_std=gps_noise_std,
            accel_noise_std=accel_noise_std
        )

        # Calibration for accelerometer (magnitude-based, orientation-independent)
        self.accel_bias = np.array([0.0, 0.0, 0.0])
        self.accel_gravity_magnitude = self.g

        # Thread safety for overall fusion
        self.lock = threading.Lock()

    def calibrate_accelerometer(self, accel_samples: np.ndarray) -> None:
        """
        Calibrate accelerometer using samples collected while device is still.

        Args:
            accel_samples: Array of shape (N, 3) with N accel readings [ax, ay, az]
        """
        with self.lock:
            # Bias: mean of stationary samples
            self.accel_bias = np.mean(accel_samples, axis=0)

            # Gravity magnitude: magnitude of bias vector
            # (when device is still, accel = gravity)
            self.accel_gravity_magnitude = np.linalg.norm(self.accel_bias)

            print(f"✓ Accelerometer calibrated")
            print(f"  Bias: {self.accel_bias}")
            print(f"  Gravity magnitude: {self.accel_gravity_magnitude:.3f} m/s²")

    def update(self, gyro: np.ndarray, accel: np.ndarray,
               mag: np.ndarray, gps_data: Optional[Dict] = None) -> None:
        """
        Update fusion with new sensor readings.

        Args:
            gyro: [gx, gy, gz] angular velocity in rad/s
            accel: [ax, ay, az] raw acceleration in m/s²
            mag: [mx, my, mz] magnetometer readings
            gps_data: Optional dict with keys 'lat', 'lon'
        """
        with self.lock:
            # Stage 1: Predict and update IMU Kalman
            self.imu_kalman.predict(gyro)
            self.imu_kalman.update(accel, mag)

            # Get device orientation
            roll, pitch, yaw = self.imu_kalman.get_orientation()

            # Stage 2a: Remove gravity from accelerometer
            # Gravity vector in device frame based on orientation
            g_device = np.array([
                self.accel_gravity_magnitude * math.sin(pitch),
                self.accel_gravity_magnitude * math.sin(roll) * math.cos(pitch),
                self.accel_gravity_magnitude * math.cos(roll) * math.cos(pitch)
            ])

            # Calibrate and remove gravity
            accel_calibrated = accel - self.accel_bias
            accel_motion = accel_calibrated - g_device

            # Stage 2b: Transform to world frame
            # Device frame → World frame rotation (about Z-axis by yaw)
            cos_yaw = math.cos(yaw)
            sin_yaw = math.sin(yaw)

            accel_world_x = accel_motion[0] * cos_yaw - accel_motion[1] * sin_yaw
            accel_world_y = accel_motion[0] * sin_yaw + accel_motion[1] * cos_yaw
            accel_world = np.array([accel_world_x, accel_world_y])

            # Stage 2c: Update motion Kalman
            self.motion_kalman.predict()
            self.motion_kalman.update_accelerometer(accel_world)

            # Stage 2d: Update with GPS if available
            if gps_data and 'lat' in gps_data and 'lon' in gps_data:
                self.motion_kalman.update_gps(gps_data['lat'], gps_data['lon'])

    def get_state(self) -> Dict:
        """
        Get complete state estimate.

        Returns:
            Dict with all relevant states
        """
        with self.lock:
            roll, pitch, yaw = self.imu_kalman.get_orientation()
            motion_state = self.motion_kalman.get_state()

            return {
                'orientation': {
                    'roll': math.degrees(roll),
                    'pitch': math.degrees(pitch),
                    'yaw': math.degrees(yaw),
                    'roll_rad': roll,
                    'pitch_rad': pitch,
                    'yaw_rad': yaw
                },
                'position': {
                    'x': motion_state['x'],
                    'y': motion_state['y'],
                    'distance': math.sqrt(motion_state['x']**2 + motion_state['y']**2)
                },
                'velocity': {
                    'x': motion_state['vel_x'],
                    'y': motion_state['vel_y'],
                    'magnitude': math.sqrt(motion_state['vel_x']**2 + motion_state['vel_y']**2)
                },
                'acceleration': {
                    'x': motion_state['acc_x'],
                    'y': motion_state['acc_y'],
                    'magnitude': math.sqrt(motion_state['acc_x']**2 + motion_state['acc_y']**2)
                }
            }


if __name__ == "__main__":
    print("✓ Two-stage Kalman fusion system loaded")
    print("  Use: from kalman_2d_independent import TwoStageKalmanFusion")
