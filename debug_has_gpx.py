#!/usr/bin/env python3
"""Debug has_gpx detection logic"""

import json
import os

filepath = "/data/data/com.termux/files/home/gojo/motion_tracker_sessions/comparison_20251105_121921.json"
filename = "comparison_20251105_121921.json"
gpx_filepath = filepath.replace(".json", ".gpx").replace(".json.gz", ".gpx")

print(f"File: {filename}")
print(f"GPX path: {gpx_filepath}")
print(f"GPX exists: {os.path.exists(gpx_filepath)}")
print()

# Load JSON
with open(filepath) as f:
    data = json.load(f)

print(f"JSON keys: {list(data.keys())}")
print()

# Check has_gpx logic
has_gpx = os.path.exists(gpx_filepath)
print(f"Initial has_gpx (from file): {has_gpx}")

if not has_gpx:
    print("\nChecking JSON for GPS data...")

    # Check format 1: gps_data
    if "gps_data" in data:
        print(f"  Found 'gps_data' key")
        if isinstance(data["gps_data"], list):
            print(f"    gps_data is list with {len(data['gps_data'])} items")
            if len(data["gps_data"]) > 0:
                print(f"    First item: {data['gps_data'][0]}")
                for sample in data["gps_data"]:
                    if "gps" in sample and isinstance(sample["gps"], dict):
                        if "latitude" in sample["gps"] and "longitude" in sample["gps"]:
                            has_gpx = True
                            print(f"    ✓ Found valid GPS data in gps_data format")
                            break
    else:
        print(f"  No 'gps_data' key")

    # Check format 2: gps_samples
    if not has_gpx and "gps_samples" in data:
        print(f"  Found 'gps_samples' key")
        if isinstance(data["gps_samples"], list):
            print(f"    gps_samples is list with {len(data['gps_samples'])} items")
            if len(data["gps_samples"]) > 0:
                first_sample = data["gps_samples"][0]
                print(f"    First sample type: {type(first_sample)}")
                print(f"    First sample keys: {list(first_sample.keys()) if isinstance(first_sample, dict) else 'not a dict'}")
                print(f"    First sample: {first_sample}")

                for idx, sample in enumerate(data["gps_samples"]):
                    if isinstance(sample, dict):
                        # Check nested format
                        if "gps" in sample and isinstance(sample["gps"], dict):
                            if "latitude" in sample["gps"] and "longitude" in sample["gps"]:
                                has_gpx = True
                                print(f"    ✓ Found valid GPS data in nested format (sample {idx})")
                                break
                        # Check flat format
                        elif "latitude" in sample and "longitude" in sample:
                            has_gpx = True
                            print(f"    ✓ Found valid GPS data in flat format (sample {idx})")
                            print(f"       Sample: {sample}")
                            break
                else:
                    print(f"    ✗ No valid GPS data found in {len(data['gps_samples'])} samples")
    else:
        print(f"  No 'gps_samples' key or already found GPS data")

print(f"\nFinal has_gpx: {has_gpx}")
