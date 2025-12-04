import json
import gzip
import sys

def check_gaps(file_path):
    print(f"Analyzing {file_path} for GPS gaps...")
    try:
        with gzip.open(file_path, 'rt') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error opening file: {e}")
        return

    readings = data.get("readings", [])
    gps_timestamps = []

    for r in readings:
        if r.get("gps"):
            # Use the GPS reading's own timestamp if available, or the wrapper timestamp
            ts = r["gps"].get("timestamp") or r.get("timestamp")
            if ts:
                gps_timestamps.append(ts)

    if not gps_timestamps:
        print("No GPS data found.")
        return

    gps_timestamps.sort() 
    
    gaps = []
    max_gap = 0.0
    total_duration = gps_timestamps[-1] - gps_timestamps[0]
    
    print(f"Total duration: {total_duration:.1f}s")
    print(f"Number of GPS points: {len(gps_timestamps)}")

    for i in range(1, len(gps_timestamps)):
        delta = gps_timestamps[i] - gps_timestamps[i-1]
        if delta > 3.0:
            gaps.append((gps_timestamps[i-1], gps_timestamps[i], delta))
        max_gap = max(max_gap, delta)

    readings_timestamps = []
    for r in readings:
        if r.get("timestamp"):
            readings_timestamps.append(r["timestamp"])
            
    readings_timestamps.sort()
    
    print(f"\n--- DATA STREAM ANALYSIS ---")
    print(f"Total readings: {len(readings_timestamps)}")
    
    reading_gaps = []
    max_reading_gap = 0.0
    
    for i in range(1, len(readings_timestamps)):
        delta = readings_timestamps[i] - readings_timestamps[i-1]
        if delta > 1.0: # Report any silence > 1s
            reading_gaps.append((readings_timestamps[i-1], readings_timestamps[i], delta))
        max_reading_gap = max(max_reading_gap, delta)
        
    if reading_gaps:
        print(f"⚠️ FOUND {len(reading_gaps)} DATA CUTOUTS (> 1.0s) where app was likely suspended:")
        for start, end, duration in reading_gaps:
            print(f"  - Silence of {duration:.1f}s at {start:.1f}")
    else:
        print("✅ Continuous data stream (app stayed alive).")

    print(f"\nMax GPS gap: {max_gap:.3f}s")
    print(f"Max Data gap: {max_reading_gap:.3f}s")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python check_gaps.py <file.json.gz>")
    else:
        check_gaps(sys.argv[1])
