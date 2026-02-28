"""
Quick sanity check for Gojo Logger data.
Verifies timestamp alignment between GPS and IMU streams.

Usage:
    python verify_data.py /path/to/gojo_XXXXXXXX_XXXXXX/
"""

import sys
import csv
from pathlib import Path


def load_csv(path):
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        return [row for row in reader]


def ns_to_sec(ns):
    return int(ns) / 1_000_000_000


def main():
    if len(sys.argv) < 2:
        print("Usage: python verify_data.py <session_dir>")
        sys.exit(1)

    session = Path(sys.argv[1])
    imu_path = session / "imu.csv"
    gps_path = session / "gps.csv"
    meta_path = session / "metadata.txt"

    if not imu_path.exists() or not gps_path.exists():
        print(f"Missing files in {session}")
        sys.exit(1)

    # Load metadata
    meta = {}
    if meta_path.exists():
        for line in meta_path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                meta[k.strip()] = v.strip()
        print(f"Device: {meta.get('device', '?')}")
        print(f"Session start: {meta.get('session_start_utc_ms', '?')}")
        print()

    # Load data
    imu = load_csv(imu_path)
    gps = load_csv(gps_path)

    print(f"IMU samples: {len(imu)}")
    print(f"GPS samples: {len(gps)}")
    print()

    if not imu or not gps:
        print("No data to analyze.")
        return

    # Time ranges
    imu_start = int(imu[0]["elapsed_nanos"])
    imu_end = int(imu[-1]["elapsed_nanos"])
    gps_start = int(gps[0]["elapsed_nanos"])
    gps_end = int(gps[-1]["elapsed_nanos"])

    print("=== TIMESTAMP RANGES (seconds since boot) ===")
    print(f"IMU: {ns_to_sec(imu_start):.3f} -> {ns_to_sec(imu_end):.3f}  "
          f"(duration: {ns_to_sec(imu_end - imu_start):.1f}s)")
    print(f"GPS: {ns_to_sec(gps_start):.3f} -> {ns_to_sec(gps_end):.3f}  "
          f"(duration: {ns_to_sec(gps_end - gps_start):.1f}s)")
    print()

    # OVERLAP CHECK — the whole reason this app exists
    overlap_start = max(imu_start, gps_start)
    overlap_end = min(imu_end, gps_end)

    if overlap_end > overlap_start:
        overlap_sec = ns_to_sec(overlap_end - overlap_start)
        total_sec = ns_to_sec(max(imu_end, gps_end) - min(imu_start, gps_start))
        pct = (overlap_sec / total_sec) * 100
        print(f"✓ OVERLAP: {overlap_sec:.1f}s ({pct:.1f}% of session)")
    else:
        gap_sec = ns_to_sec(overlap_start - overlap_end)
        print(f"✗ NO OVERLAP — gap of {gap_sec:.1f}s between streams!")
        print("  Something is wrong with timestamp alignment.")
    print()

    # Accel/gyro split
    accel = [r for r in imu if r["sensor_type"] == "accel"]
    gyro = [r for r in imu if r["sensor_type"] == "gyro"]
    print(f"Accel samples: {len(accel)}")
    print(f"Gyro samples:  {len(gyro)}")

    # Effective sample rates
    if len(accel) > 1:
        dt = (int(accel[-1]["elapsed_nanos"]) - int(accel[0]["elapsed_nanos"])) / 1e9
        if dt > 0:
            print(f"Accel effective rate: {len(accel) / dt:.1f} Hz")
    if len(gyro) > 1:
        dt = (int(gyro[-1]["elapsed_nanos"]) - int(gyro[0]["elapsed_nanos"])) / 1e9
        if dt > 0:
            print(f"Gyro effective rate:  {len(gyro) / dt:.1f} Hz")
    if len(gps) > 1:
        dt = (int(gps[-1]["elapsed_nanos"]) - int(gps[0]["elapsed_nanos"])) / 1e9
        if dt > 0:
            print(f"GPS effective rate:   {len(gps) / dt:.1f} Hz")

    print()
    print("First 5 GPS fixes:")
    for row in gps[:5]:
        t = ns_to_sec(int(row["elapsed_nanos"]))
        print(f"  t={t:.3f}s  ({row['lat']}, {row['lon']})  "
              f"acc={row['accuracy_m']}m  sats={row['satellites']}")


if __name__ == "__main__":
    main()
