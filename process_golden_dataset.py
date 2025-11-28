import json
import csv
import math
import gzip

# Earth radius in meters
R_EARTH = 6378137.0

def local_to_global(lat_ref, lon_ref, north_m, east_m):
    """Converts local meters (North/East) back to Lat/Lon."""
    # Offset in radians
    d_lat = north_m / R_EARTH
    d_lon = east_m / (R_EARTH * math.cos(math.radians(lat_ref)))

    # New Lat/Lon
    new_lat = lat_ref + math.degrees(d_lat)
    new_lon = lon_ref + math.degrees(d_lon)
    return new_lat, new_lon

input_file = 'motion_tracker_sessions/golden/comparison_20251125_155300.json.gz'
print(f"Processing {input_file}...")

try:
    with gzip.open(input_file, 'rt', encoding='utf-8') as f:
        data = json.load(f)
except FileNotFoundError:
    print(f"Error: File {input_file} not found.")
    exit(1)

readings = data.get('readings', [])
output_rows = []

# Find the Datum (First valid GPS point)
datum_lat = None
datum_lon = None

for r in readings:
    if r.get('gps') and r['gps'].get('latitude') is not None:
        datum_lat = r['gps']['latitude']
        datum_lon = r['gps']['longitude']
        print(f"Datum found: {datum_lat}, {datum_lon}")
        break

if datum_lat is None:
    print("Error: No GPS data found to set datum.")
    exit(1)

# Process rows
for r in readings:
    ts = r.get('timestamp', 0)
    
    # RAW GPS
    gps = r.get('gps')
    # Handle case where gps might be None
    if gps:
        raw_lat = gps.get('latitude', '') 
        raw_lon = gps.get('longitude', '')
    else:
        raw_lat = ''
        raw_lon = ''
    
    # RAW ACCEL (Handling nulls)
    raw_acc_x = 0.0
    raw_acc_y = 0.0
    
    # EKF DATA (Experimental 13d)
    ekf_data = r.get('experimental_13d', {})
    if ekf_data is None:
        ekf_data = {}
        
    ekf_pos = ekf_data.get('position', [0,0,0]) # Assuming [x, y, z] list
    ekf_vel = ekf_data.get('velocity', [0,0,0]) # Assuming [vx, vy, vz] list
    
    # Convert EKF Local Meters -> Global Lat/Lon
    # Assuming index 0=East(x), 1=North(y) (ENU Convention)
    # If your EKF is NED, swap logic: 0=North, 1=East
    if ekf_pos:
        east_m = ekf_pos[0]
        north_m = ekf_pos[1]
        
        ekf_lat_val, ekf_lon_val = local_to_global(datum_lat, datum_lon, north_m, east_m)
    else:
        # Fallback if position is missing/empty
        ekf_lat_val = datum_lat
        ekf_lon_val = datum_lon

    # Handle case where velocity might be None or empty
    if not ekf_vel:
        ekf_vel = [0.0, 0.0, 0.0]

    output_rows.append([
        ts, 
        raw_lat, raw_lon, 
        raw_acc_x, raw_acc_y, 
        ekf_lat_val, ekf_lon_val, 
        ekf_vel[0], ekf_vel[1]
    ])

# Write CSV
headers = ['timestamp', 'raw_lat', 'raw_lon', 'raw_accel_x', 'raw_accel_y', 
           'ekf_lat', 'ekf_lon', 'ekf_vel_x', 'ekf_vel_y']

with open('drive_log.csv', 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(headers)
    writer.writerows(output_rows)

print(f"Successfully converted {len(output_rows)} readings to drive_log.csv")
