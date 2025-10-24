
# This script requires the following packages:
# pip install numpy matplotlib filterpy

import numpy as np
import matplotlib.pyplot as plt
from filterpy.kalman import KalmanFilter
from filterpy.common import Q_discrete_white_noise

# --- 1. Define Constants ---
dt = 0.1  # Time step
num_steps = 500

# Noise parameters
gps_noise_std = 5.0  # GPS position noise standard deviation (meters)
accel_noise_std = 0.5  # Accelerometer noise standard deviation (m/s^2)

# --- 2. Simulate True Path ---
# State: [pos_x, vel_x, acc_x, pos_y, vel_y, acc_y]
true_states = np.zeros((num_steps, 6))
true_pos = np.zeros((num_steps, 2))
true_accel = np.zeros((num_steps, 2))

# Initial conditions
true_states[0] = np.array([0., 0., 0., 0., 0., 0.]) # x, vx, ax, y, vy, ay

# Simple motion model: constant velocity then a turn
for i in range(1, num_steps):
    if i < num_steps / 3: # Constant velocity
        true_states[i, 1] = 10.0 # vx = 10 m/s
        true_states[i, 4] = 0.0  # vy = 0 m/s
    elif i < 2 * num_steps / 3: # Turn
        true_states[i, 1] = 10.0 * np.cos((i - num_steps/3) * dt * 0.1)
        true_states[i, 4] = 10.0 * np.sin((i - num_steps/3) * dt * 0.1)
        true_states[i, 2] = -10.0 * 0.1 * np.sin((i - num_steps/3) * dt * 0.1) # ax
        true_states[i, 5] = 10.0 * 0.1 * np.cos((i - num_steps/3) * dt * 0.1) # ay
    else: # Constant velocity again
        true_states[i, 1] = true_states[2*num_steps//3 -1, 1]
        true_states[i, 4] = true_states[2*num_steps//3 -1, 4]

    # Integrate acceleration to get velocity, and velocity to get position
    true_states[i, 0] = true_states[i-1, 0] + true_states[i, 1] * dt + 0.5 * true_states[i, 2] * dt**2
    true_states[i, 3] = true_states[i-1, 3] + true_states[i, 4] * dt + 0.5 * true_states[i, 5] * dt**2

    true_states[i, 1] = true_states[i-1, 1] + true_states[i, 2] * dt
    true_states[i, 4] = true_states[i-1, 4] + true_states[i, 5] * dt

    true_pos[i] = [true_states[i, 0], true_states[i, 3]]
    true_accel[i] = [true_states[i, 2], true_states[i, 5]]


# --- 3. Simulate Noisy Sensor Data ---
# GPS measurements (position only)
gps_measurements = true_pos + np.random.normal(0, gps_noise_std, true_pos.shape)

# Accelerometer measurements (acceleration only)
accel_measurements = true_accel + np.random.normal(0, accel_noise_std, true_accel.shape)


# --- 4. Initialize Kalman Filter ---
# State vector: [pos_x, vel_x, acc_x, pos_y, vel_y, acc_y]
# We are tracking 6 variables: 3 for X-axis (position, velocity, acceleration) and 3 for Y-axis
kf = KalmanFilter(dim_x=6, dim_z=4) # dim_z = 4 because we measure pos_x, pos_y, acc_x, acc_y

# Initial state (x) - start with true initial position, zero velocity and acceleration
kf.x = np.array([true_pos[0, 0], 0., 0., true_pos[0, 1], 0., 0.])

# State transition matrix (F) - Constant acceleration model
# x = x0 + v0*dt + 0.5*a*dt^2
# v = v0 + a*dt
# a = a0 (constant)
kf.F = np.array([[1, dt, 0.5*dt**2, 0,  0,         0        ],
                 [0, 1,  dt,        0,  0,         0        ],
                 [0, 0,  1,         0,  0,         0        ],
                 [0, 0,  0,         1,  dt,        0.5*dt**2],
                 [0, 0,  0,         0,  1,         dt       ],
                 [0, 0,  0,         0,  0,         1        ]])

# Process noise covariance (Q)
# This represents the uncertainty in our state transition model.
# We assume some noise in acceleration.
q_std_accel = 0.1 # Standard deviation of process noise for acceleration
kf.Q = Q_discrete_white_noise(dim=3, dt=dt, var=q_std_accel**2, block_size=2)
# Q_discrete_white_noise creates a block diagonal matrix for each dimension.
# For a 6D state (px,vx,ax,py,vy,ay), with white noise on acceleration,
# we need a 3D block for (p,v,a) repeated for x and y.
# The function Q_discrete_white_noise(dim=3, dt=dt, var=q_std_accel**2) creates a 3x3 matrix for (p,v,a)
# block_size=2 repeats this for x and y.

# Measurement matrix (H)
# We measure position (GPS) and acceleration (accelerometer)
# Measurements: [gps_pos_x, gps_pos_y, accel_x, accel_y]
kf.H = np.array([[1, 0, 0, 0, 0, 0], # Measure pos_x
                 [0, 0, 0, 1, 0, 0], # Measure pos_y
                 [0, 0, 1, 0, 0, 0], # Measure acc_x
                 [0, 0, 0, 0, 0, 1]]) # Measure acc_y

# Measurement noise covariance (R)
# R for GPS (first two elements) and accelerometer (last two elements)
kf.R = np.diag([gps_noise_std**2, gps_noise_std**2, accel_noise_std**2, accel_noise_std**2])

# Initial state covariance (P) - high uncertainty initially
kf.P *= 1000.

# --- 5. Run Kalman Filter ---
filtered_states = np.zeros((num_steps, 6))
for i in range(num_steps):
    # Create combined measurement vector
    # z = [gps_x, gps_y, accel_x, accel_y]
    z = np.array([gps_measurements[i, 0],
                  gps_measurements[i, 1],
                  accel_measurements[i, 0],
                  accel_measurements[i, 1]])

    kf.predict()
    kf.update(z)
    filtered_states[i] = kf.x

# --- 6. Plot Results ---
plt.figure(figsize=(12, 8))

plt.plot(true_pos[:, 0], true_pos[:, 1], 'g-', label='True Path')
plt.plot(gps_measurements[:, 0], gps_measurements[:, 1], 'rx', markersize=3, label='Noisy GPS Measurements')
plt.plot(filtered_states[:, 0], filtered_states[:, 3], 'b-', label='Kalman Filter Estimate')

plt.xlabel('X Position (m)')
plt.ylabel('Y Position (m)')
plt.title('Kalman Filter for GPS and Accelerometer Fusion')
plt.legend()
plt.grid(True)
plt.axis('equal')
plt.show()

# Plot individual components (e.g., X position)
plt.figure(figsize=(12, 6))
plt.plot(true_pos[:, 0], 'g-', label='True X Position')
plt.plot(gps_measurements[:, 0], 'rx', markersize=3, label='Noisy GPS X Measurement')
plt.plot(filtered_states[:, 0], 'b-', label='Kalman Filter X Estimate')
plt.xlabel('Time Step')
plt.ylabel('Y Position (m)')
plt.title('X Position: True vs. Noisy GPS vs. Kalman Filter')
plt.legend()
plt.grid(True)
plt.show()

plt.figure(figsize=(12, 6))
plt.plot(true_states[:, 2], 'g-', label='True X Acceleration')
plt.plot(accel_measurements[:, 0], 'rx', markersize=3, label='Noisy Accelerometer X Measurement')
plt.plot(filtered_states[:, 2], 'b-', label='Kalman Filter X Acceleration Estimate')
plt.xlabel('Time Step')
plt.ylabel('X Acceleration (m/s^2)')
plt.title('X Acceleration: True vs. Noisy Accelerometer vs. Kalman Filter')
plt.legend()
plt.grid(True)
plt.show()
