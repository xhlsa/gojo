#!/usr/bin/env python3
import sys
sys.path.insert(0, '/data/data/com.termux/files/home/gojo')

from motion_tracker_v2.filters.ekf import ExtendedKalmanFilter

filt = ExtendedKalmanFilter(gps_noise_std=5.0)

origin_lat, origin_lon = 40.7128, -74.0060
filt.update_gps(origin_lat, origin_lon, gps_speed=0.0, gps_accuracy=5.0)

jitter_pattern = [
    (+0.00004, -0.00002),
    (-0.00003, +0.00001),
    (+0.00002, +0.00003),
    (-0.00005, 0.00000),
    (0.00001, -0.00004),
    (+0.00003, +0.00002),
    (-0.00002, -0.00003),
    (+0.00004, +0.00001),
    (-0.00001, +0.00004),
    (0.00002, -0.00003),
]

last_lat, last_lon = origin_lat, origin_lon

print("Jitter Pattern Debug:")
print("-" * 70)
for i, (dlat, dlon) in enumerate(jitter_pattern, 1):
    new_lat = origin_lat + dlat
    new_lon = origin_lon + dlon

    # Calculate haversine distance from previous point
    dist = filt.haversine_distance(last_lat, last_lon, new_lat, new_lon)

    v, d = filt.update_gps(new_lat, new_lon, gps_speed=0.0, gps_accuracy=5.0)

    # Distance that should be accumulated after noise floor
    true_movement = max(0.0, dist - 5.0)

    print(f"Update {i}: raw_dist={dist:.2f}m, noise_floor=5.0m, accumulated={true_movement:.2f}m | total={d:.2f}m")

    last_lat, last_lon = new_lat, new_lon
