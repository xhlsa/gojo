#!/usr/bin/env python3
"""
Generate GPX file from comparison JSON for map viewing
"""
import json
import gzip
from datetime import datetime
from pathlib import Path

def generate_gpx(json_filepath: str, output_filepath: str = None):
    """Generate GPX from comparison JSON file"""

    # Load JSON
    print(f"Loading: {json_filepath}")
    if json_filepath.endswith('.gz'):
        with gzip.open(json_filepath, 'rt') as f:
            data = json.load(f)
    else:
        with open(json_filepath, 'r') as f:
            data = json.load(f)

    # Extract GPS points (flat format used by comparison files)
    gps_samples = data.get('gps_samples', [])
    print(f"Found {len(gps_samples)} GPS samples")

    gps_points = []
    for sample in gps_samples:
        if isinstance(sample, dict) and 'latitude' in sample and 'longitude' in sample:
            gps_points.append({
                'lat': sample['latitude'],
                'lon': sample['longitude'],
                'ele': sample.get('altitude', 0),
                'time': sample.get('timestamp', '')
            })

    print(f"Extracted {len(gps_points)} GPS coordinates")

    if not gps_points:
        print("ERROR: No GPS coordinates found!")
        return

    # Generate GPX content
    gpx = '<?xml version="1.0" encoding="UTF-8"?>\n'
    gpx += '<gpx version="1.1" creator="Motion Tracker Dashboard" xmlns="http://www.topografix.com/GPX/1/1">\n'
    gpx += '  <metadata>\n'
    gpx += f'    <time>{datetime.now().isoformat()}Z</time>\n'
    gpx += '  </metadata>\n'
    gpx += '  <trk>\n'
    gpx += '    <name>Drive Track</name>\n'
    gpx += '    <trkseg>\n'

    for point in gps_points:
        gpx += f'      <trkpt lat="{point["lat"]}" lon="{point["lon"]}">\n'
        if point['ele']:
            gpx += f'        <ele>{point["ele"]}</ele>\n'
        if point['time']:
            gpx += f'        <time>{point["time"]}Z</time>\n'
        gpx += '      </trkpt>\n'

    gpx += '    </trkseg>\n'
    gpx += '  </trk>\n'
    gpx += '</gpx>\n'

    # Determine output path
    if output_filepath is None:
        output_filepath = json_filepath.replace('.json.gz', '.gpx').replace('.json', '.gpx')

    # Write GPX file
    with open(output_filepath, 'w') as f:
        f.write(gpx)

    print(f"GPX saved to: {output_filepath}")
    print(f"First point: {gps_points[0]['lat']}, {gps_points[0]['lon']}")
    print(f"Last point: {gps_points[-1]['lat']}, {gps_points[-1]['lon']}")

    return output_filepath

if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 generate_gpx_for_comparison.py <json_file>")
        print("Example: python3 generate_gpx_for_comparison.py motion_tracker_sessions/comparison_20251104_165024.json.gz")
        sys.exit(1)

    json_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    generate_gpx(json_file, output_file)
