import pandas as pd
import numpy as np

# Configuration for dummy data
num_points = 200
start_lat, start_lon = 32.2226, -110.9747 # Somewhere in Tucson

# Initialize lists for data
timestamps = np.arange(num_points)
raw_lats = []
raw_lons = []
ekf_lats = []
ekf_lons = []
raw_accel_x = np.random.normal(0, 0.1, num_points)
raw_accel_y = np.random.normal(0, 0.1, num_points)
ekf_vel_x = []
ekf_vel_y = []

current_lat_ekf = start_lat
current_lon_ekf = start_lon
current_vel_x = 0.0
current_vel_y = 0.0

# Simulate movement
for i in range(num_points):
    # Simulate a stop for a period, then acceleration
    if i < 50: # Accelerate
        current_vel_x += 0.05
        current_vel_y += 0.03
    elif i < 100: # Constant velocity
        pass
    elif i < 150: # Decelerate to stop
        current_vel_x *= 0.9
        current_vel_y *= 0.9
        if abs(current_vel_x) < 0.01: current_vel_x = 0.0
        if abs(current_vel_y) < 0.01: current_vel_y = 0.0
    else: # Accelerate again
        current_vel_x += 0.02
        current_vel_y -= 0.01

    # Update EKF position
    current_lat_ekf += current_vel_y * 0.00001 # Scale velocity to degrees
    current_lon_ekf += current_vel_x * 0.00001

    ekf_lats.append(current_lat_ekf)
    ekf_lons.append(current_lon_ekf)
    ekf_vel_x.append(current_vel_x)
    ekf_vel_y.append(current_vel_y)

    # Add noise for raw GPS
    raw_lats.append(current_lat_ekf + np.random.normal(0, 0.00005))
    raw_lons.append(current_lon_ekf + np.random.normal(0, 0.00005))

# Create DataFrame
df_dummy = pd.DataFrame({
    'timestamp': timestamps,
    'raw_lat': raw_lats,
    'raw_lon': raw_lons,
    'raw_accel_x': raw_accel_x,
    'raw_accel_y': raw_accel_y,
    'ekf_lat': ekf_lats,
    'ekf_lon': ekf_lons,
    'ekf_vel_x': ekf_vel_x,
    'ekf_vel_y': ekf_vel_y
})

# Save to CSV
df_dummy.to_csv('drive_log.csv', index=False)

print("Dummy drive_log.csv created successfully.")