import json
import csv
import math
import gzip
import glob
import os
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import folium

# Configuration
INPUT_DIR = 'motion_tracker_rs/motion_tracker_sessions/' # Changed for replay sessions
OUTPUT_DIR = os.path.expanduser('~/storage/downloads/') # Output to main downloads folder
R_EARTH = 6378137.0

def local_to_global(lat_ref, lon_ref, north_m, east_m):
    """Converts local meters (North/East) back to Lat/Lon."""
    d_lat = north_m / R_EARTH
    d_lon = east_m / (R_EARTH * math.cos(math.radians(lat_ref)))
    return lat_ref + math.degrees(d_lat), lon_ref + math.degrees(d_lon)

def process_file(file_path, output_prefix="report_"):
    filename = os.path.basename(file_path)
    
    # Determine if gzipped or plain JSON and extract timestamp
    is_gzipped = filename.endswith('.gz')
    base_name = filename.replace('comparison_', '').replace('.json.gz', '').replace('.json', '')
    timestamp_str = base_name.split('_final')[0] # Handle _final suffix
    
    print(f"Processing {filename} (ID: {timestamp_str})...")

    # --- 1. LOAD & PARSE JSON ---
    try:
        if is_gzipped:
            with gzip.open(file_path, 'rt', encoding='utf-8') as f:
                data = json.load(f)
        else:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
    except Exception as e:
        print(f"Failed to read {filename}: {e}")
        return

    readings = data.get('readings', [])
    if not readings:
        print("No readings found.")
        return

    # Find Datum
    datum_lat = None
    datum_lon = None
    for r in readings:
        if r.get('gps') and r['gps'].get('latitude') is not None:
            datum_lat = r['gps']['latitude']
            datum_lon = r['gps']['longitude']
            break
    
    if datum_lat is None:
        print("No GPS datum found, skipping.")
        return

    # Convert to DataFrame-friendly list
    rows = []
    for r in readings:
        ts = r.get('timestamp', 0)
        gps = r.get('gps')
        if gps:
            raw_lat = gps.get('latitude', np.nan)
            raw_lon = gps.get('longitude', np.nan)
        else:
            raw_lat = np.nan
            raw_lon = np.nan
            
        ekf_data = r.get('experimental_13d', {}) or {}
        ekf_pos = ekf_data.get('position', [0,0,0]) or [0,0,0]
        ekf_vel = ekf_data.get('velocity', [0,0,0]) or [0,0,0]
        
        # Local -> Global
        if ekf_pos and pd.notna(datum_lat) and pd.notna(datum_lon): # Ensure datum is valid for conversion
             ekf_lat_val, ekf_lon_val = local_to_global(datum_lat, datum_lon, ekf_pos[1], ekf_pos[0])
        else:
             ekf_lat_val, ekf_lon_val = np.nan, np.nan

        rows.append({
            'timestamp': ts,
            'raw_lat': raw_lat,
            'raw_lon': raw_lon,
            'ekf_lat': ekf_lat_val,
            'ekf_lon': ekf_lon_val,
            'ekf_vel_x': ekf_vel[0],
            'ekf_vel_y': ekf_vel[1]
        })

    df = pd.DataFrame(rows)
    df['ekf_speed'] = np.sqrt(df['ekf_vel_x']**2 + df['ekf_vel_y']**2)
    df['has_gps'] = df['raw_lat'].notna()

    # --- 2. GENERATE PLOTS ---
    
    # Plot 1: Failure Report (Velocity vs GPS)
    plt.figure(figsize=(12, 8))
    plt.subplot(2, 1, 1)
    plt.title(f"Forensics: {timestamp_str}")
    plt.plot(df['timestamp'], df['ekf_speed'], 'b-', label='EKF Speed')
    plt.ylabel("Speed (m/s)")
    plt.grid(True)
    plt.legend()

    plt.subplot(2, 1, 2)
    plt.plot(df['timestamp'], df['has_gps'], 'g-', label='GPS Available')
    plt.fill_between(df['timestamp'], 0, 1, where=~df['has_gps'], color='red', alpha=0.3, label='GPS Lost')
    plt.ylabel("GPS Fix")
    plt.xlabel("Time (s)")
    plt.legend()
    plt.grid(True)
    
    out_fail = os.path.join(OUTPUT_DIR, f"{output_prefix}{timestamp_str}_failure.png")
    plt.savefig(out_fail)
    plt.close()

    # Plot 2: Valid Trajectory (Clipped < 60m/s)
    df_clean = df[df['ekf_speed'] < 60]
    df_clean_gps = df_clean.dropna(subset=['raw_lat', 'raw_lon'])
    df_clean_ekf = df_clean.dropna(subset=['ekf_lat', 'ekf_lon'])

    plt.figure(figsize=(10, 10))
    plt.title(f"Valid Trajectory: {timestamp_str}")
    if not df_clean_gps.empty:
        plt.scatter(df_clean_gps['raw_lon'], df_clean_gps['raw_lat'], c='red', alpha=0.3, s=2, label='Raw GPS')
    if not df_clean_ekf.empty:
        plt.plot(df_clean_ekf['ekf_lon'], df_clean_ekf['ekf_lat'], c='blue', linewidth=2, label='EKF')
    
    plt.xlabel("Longitude")
    plt.ylabel("Latitude")
    plt.legend()
    plt.grid(True)
    
    out_traj = os.path.join(OUTPUT_DIR, f"{output_prefix}{timestamp_str}_trajectory.png")
    plt.savefig(out_traj)
    plt.close()

    print(f"Generated {out_fail} and {out_traj}")

def main():
    json_files = glob.glob(os.path.join(INPUT_DIR, 'comparison_*.json'))
    json_gz_files = glob.glob(os.path.join(INPUT_DIR, 'comparison_*.json.gz'))
    
    all_files = sorted(json_files + json_gz_files)
    
    print(f"Found {len(all_files)} replay sessions.")
    for f in all_files:
        process_file(f, output_prefix="replay_report_")

if __name__ == '__main__':
    main()