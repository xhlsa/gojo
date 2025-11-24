#!/usr/bin/env python3
"""
Lazarus Protocol: Stitch 142 auto-save files into single full_drive.json

Deduplicates overlapping timestamps using hash map strategy.
Later files overwrite earlier ones (ensures latest filter estimates).
"""

import json
import gzip
import sys
from pathlib import Path
from collections import OrderedDict

def stitch_drive_data(session_dir: str, output_path: str, session_pattern: str):
    """
    Stitch all auto-save files from a drive session into one complete dataset.

    Args:
        session_dir: Directory containing comparison_*.json.gz files
        output_path: Output path for stitched full_drive.json.gz
        session_pattern: Pattern to match files (e.g., "comparison_20251124_15*.json.gz")
    """
    session_path = Path(session_dir)

    # Step 1: Glob & Sort by filename (timestamp embedded in name)
    files = sorted(session_path.glob(session_pattern))

    if not files:
        print(f"ERROR: No files found matching {session_pattern} in {session_dir}")
        sys.exit(1)

    print(f"Found {len(files)} auto-save files")
    print(f"First: {files[0].name}")
    print(f"Last:  {files[-1].name}")

    # Step 2: Deduplicate using HashMap keyed by timestamp
    data_map = OrderedDict()  # Preserve insertion order for efficiency
    total_readings = 0

    for i, fpath in enumerate(files, 1):
        try:
            with gzip.open(fpath, 'rt') as f:
                data = json.load(f)

            readings = data.get('readings', [])

            # Insert/overwrite records by timestamp
            for record in readings:
                ts = record.get('timestamp')
                if ts is not None:
                    data_map[ts] = record  # Later files overwrite earlier

            total_readings += len(readings)

            if i % 20 == 0:
                print(f"  Processed {i}/{len(files)} files... ({len(data_map)} unique timestamps)")

        except Exception as e:
            print(f"WARNING: Failed to read {fpath.name}: {e}")
            continue

    print(f"\nDeduplication complete:")
    print(f"  Total readings processed: {total_readings:,}")
    print(f"  Unique timestamps: {len(data_map):,}")
    print(f"  Duplicates removed: {total_readings - len(data_map):,}")

    # Step 3: Sort by timestamp and export
    sorted_readings = [data_map[ts] for ts in sorted(data_map.keys())]

    # Calculate statistics
    accel_count = sum(1 for r in sorted_readings if r.get('accel'))
    gyro_count = sum(1 for r in sorted_readings if r.get('gyro'))
    gps_count = sum(1 for r in sorted_readings if r.get('gps'))

    print(f"\nFinal dataset:")
    print(f"  Accel: {accel_count:,}")
    print(f"  Gyro: {gyro_count:,}")
    print(f"  GPS: {gps_count:,}")

    if sorted_readings:
        duration = sorted_readings[-1]['timestamp'] - sorted_readings[0]['timestamp']
        print(f"  Duration: {duration:.1f}s ({duration/60:.1f} min)")

    # Create output structure
    output_data = {
        'readings': sorted_readings,
        'metadata': {
            'source': 'stitched',
            'num_files': len(files),
            'session_pattern': session_pattern,
            'total_samples': len(sorted_readings),
            'accel_samples': accel_count,
            'gyro_samples': gyro_count,
            'gps_fixes': gps_count,
        }
    }

    # Write output
    output_path = Path(output_path)
    print(f"\nWriting to {output_path}...")

    with gzip.open(output_path, 'wt') as f:
        json.dump(output_data, f)

    # Verify file size
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"✓ Saved: {size_mb:.1f} MB")

    return output_path


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Stitch drive session auto-saves into single file')
    parser.add_argument('--session-dir', default='/data/data/com.termux/files/home/gojo/motion_tracker_sessions',
                        help='Directory containing session files')
    parser.add_argument('--pattern', default='comparison_20251124_15*.json.gz',
                        help='File pattern to match (e.g., comparison_20251124_15*.json.gz)')
    parser.add_argument('--output', default='full_drive_20251124.json.gz',
                        help='Output filename')

    args = parser.parse_args()

    output_path = Path(args.session_dir) / args.output

    print("=== Lazarus Protocol: Data Stitching ===\n")
    stitch_drive_data(args.session_dir, str(output_path), args.pattern)
    print("\n✓ Stitching complete!")
