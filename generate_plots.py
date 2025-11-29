import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import folium

# Load your data
df = pd.read_csv('drive_log.csv')

# --- Data Preprocessing ---
# Force columns to numeric, coercing errors (like empty strings) to NaN
df['raw_lat'] = pd.to_numeric(df['raw_lat'], errors='coerce')
df['raw_lon'] = pd.to_numeric(df['raw_lon'], errors='coerce')
df['ekf_lat'] = pd.to_numeric(df['ekf_lat'], errors='coerce')
df['ekf_lon'] = pd.to_numeric(df['ekf_lon'], errors='coerce')
df['ekf_vel_x'] = pd.to_numeric(df['ekf_vel_x'], errors='coerce')
df['ekf_vel_y'] = pd.to_numeric(df['ekf_vel_y'], errors='coerce')
df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')


# Calculate EKF speed magnitude from ekf_vel_x and ekf_vel_y
df['ekf_speed'] = np.sqrt(df['ekf_vel_x']**2 + df['ekf_vel_y']**2)

# Calculate Raw GPS velocity magnitude (rough approx if not logged)
# Using Haversine formula for more accurate speed calc, or simplified for flat earth
# For simplicity and to match the user's initial prompt for "flat earth",
# let's use the simplified difference calculation, but be aware of its limitations.
# The user's original script had:
# df['gps_speed_calc'] = np.sqrt(df['gps_lat'].diff()**2 + df['gps_lon'].diff()**2) * 111000 / (df['time'].diff())
# Adapting to our dummy data columns:
# Assuming 1 degree of latitude ~ 111,000 meters
# Assuming 1 degree of longitude ~ 111,000 * cos(latitude) meters
# For simplicity, let's just use a constant factor for both lat/lon diff to m.

# Use a mean latitude that is not NaN
mean_lat = df['raw_lat'].mean()
if np.isnan(mean_lat):
    mean_lat = df['ekf_lat'].mean() # Fallback to EKF if raw is all NaN (unlikely)

lat_to_m = 111000
lon_to_m = 111000 * np.cos(np.radians(mean_lat)) 

delta_lat_m = df['raw_lat'].diff() * lat_to_m
delta_lon_m = df['raw_lon'].diff() * lon_to_m
delta_time_s = df['timestamp'].diff()

# Avoid division by zero for the first row or if timestamps are identical
# Also handle cases where delta_time_s might be 0, replacing with NaN to be excluded from speed calc
delta_time_s = delta_time_s.replace(0, np.nan)


df['gps_speed_calc'] = np.sqrt(delta_lat_m**2 + delta_lon_m**2) / delta_time_s
# Fill NaN values (e.g., first row) with 0 or a reasonable initial speed
df['gps_speed_calc'] = df['gps_speed_calc'].fillna(0)


# --- PLOT 1: The Trajectory (Full / Divergent) ---
plt.figure(figsize=(10, 10))
plt.title("Sensor Fusion: GPS vs. 15-State EKF (Full Log)")

# Plot Raw GPS as noisy dots
df_gps_plot = df.dropna(subset=['raw_lat', 'raw_lon'])
print(f"Plotting {len(df_gps_plot)} GPS points")
plt.scatter(df_gps_plot['raw_lon'], df_gps_plot['raw_lat'],
            c='red', alpha=0.2, s=2, label='Raw GPS Input', zorder=5)

# Plot EKF as a smooth path
df_ekf_plot = df.dropna(subset=['ekf_lat', 'ekf_lon'])
print(f"Plotting {len(df_ekf_plot)} EKF points")
plt.plot(df_ekf_plot['ekf_lon'], df_ekf_plot['ekf_lat'],
         c='blue', linewidth=3, label='Fused Estimate (EKF)', zorder=10)

plt.xlabel("Longitude")
plt.ylabel("Latitude")
plt.legend()
plt.grid(True)
plt.savefig('engineering_plot_trajectory.png')
# plt.show()

# --- PLOT 2: Velocity Dynamics ---
plt.figure(figsize=(12, 5))
plt.title("Velocity Profile & Noise Rejection")

plt.plot(df['timestamp'], df['gps_speed_calc'], 'r-', alpha=0.4, linewidth=1, label='Raw GPS Delta Speed')
plt.plot(df['timestamp'], df['ekf_speed'], 'b-', linewidth=2, label='EKF Estimate Speed')

plt.ylabel("Speed (m/s)")
plt.xlabel("Time (s)")
plt.legend()
plt.grid(True)

plt.savefig('engineering_plot_velocity.png')
# plt.show()

# --- PLOT 3: Failure Analysis (Forensics) ---
# Detect GPS Loss
df['has_gps'] = df['raw_lat'].notna() & df['raw_lon'].notna()

plt.figure(figsize=(12, 8))
plt.subplot(2, 1, 1)
plt.title("Forensics: Velocity Runaway vs. GPS Availability")
plt.plot(df['timestamp'], df['ekf_speed'], 'b-', label='EKF Speed')
plt.ylabel("Speed (m/s)")
plt.grid(True)
plt.legend()

plt.subplot(2, 1, 2)
plt.plot(df['timestamp'], df['has_gps'], 'g-', label='GPS Available')
plt.fill_between(df['timestamp'], 0, 1, where=~df['has_gps'], color='red', alpha=0.3, label='GPS Lost (Dead Reckoning)')
plt.ylabel("GPS Fix (1/0)")
plt.xlabel("Time (s)")
plt.legend()
plt.grid(True)

plt.savefig('engineering_failure_report.png')

# --- PLOT 4: Valid Trajectory (Clipped) ---
# Filter data where speed is reasonable (e.g., < 60 m/s ~ 135 mph)
# This shows the "real" path before divergence.
df_clean = df[df['ekf_speed'] < 60]
df_clean_gps = df_clean.dropna(subset=['raw_lat', 'raw_lon'])
df_clean_ekf = df_clean.dropna(subset=['ekf_lat', 'ekf_lon'])

plt.figure(figsize=(10, 10))
plt.title("Sensor Fusion: GPS vs. EKF (Valid Data < 60m/s)")
plt.scatter(df_clean_gps['raw_lon'], df_clean_gps['raw_lat'], c='red', alpha=0.2, s=2, label='Raw GPS', zorder=5)
plt.plot(df_clean_ekf['ekf_lon'], df_clean_ekf['ekf_lat'], c='blue', linewidth=3, label='EKF (Clipped)', zorder=10)
plt.xlabel("Longitude")
plt.ylabel("Latitude")
plt.legend()
plt.grid(True)
plt.savefig('engineering_plot_trajectory_valid.png')


# --- Interactive Map (Folium) ---
# Center map on start position
start_lat = df['ekf_lat'].dropna().iloc[0] if not df['ekf_lat'].dropna().empty else 32.2226
start_lon = df['ekf_lon'].dropna().iloc[0] if not df['ekf_lon'].dropna().empty else -110.9747

m = folium.Map(location=[start_lat, start_lon], zoom_start=15)

# Add GPS points (Red)
# Use valid subset for map to avoid drawing lines to 0,0 or Infinity
if not df_clean_gps.empty:
    gps_points = list(zip(df_clean_gps['raw_lat'], df_clean_gps['raw_lon']))
    folium.PolyLine(gps_points, color="red", weight=2.5, opacity=0.5, tooltip="Raw GPS Track").add_to(m)

# Add EKF points (Blue)
if not df_clean_ekf.empty:
    ekf_points = list(zip(df_clean_ekf['ekf_lat'], df_clean_ekf['ekf_lon']))
    folium.PolyLine(ekf_points, color="blue", weight=4, opacity=0.8, tooltip="EKF Fused Track").add_to(m)

m.save("interactive_drive.html")

print("Plots and interactive map generated successfully.")
print("Generated: engineering_plot_trajectory.png, engineering_plot_velocity.png, engineering_failure_report.png, engineering_plot_trajectory_valid.png, interactive_drive.html")
