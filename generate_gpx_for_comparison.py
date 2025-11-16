#!/usr/bin/env python3
"""
Generate GPX file from comparison JSON for map viewing
"""
import json
import gzip
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from tools import replay_session as replay_mod
except ImportError:
    replay_mod = None

def _format_timestamp(value, start_dt):
    """Convert stored timestamps (float seconds or ISO strings) to ISO8601."""
    if not value:
        return None
    if isinstance(value, str):
        if value.endswith('Z'):
            return value
        return value + 'Z'
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return (start_dt + timedelta(seconds=seconds)).isoformat() + 'Z'


def _append_track(lines, name, desc, points, start_dt):
    if not points:
        return
    lines.append('  <trk>')
    lines.append(f'    <name>{name}</name>')
    lines.append(f'    <desc>{desc}</desc>')
    lines.append('    <trkseg>')
    for pt in points:
        lat = pt.get('lat')
        lon = pt.get('lon')
        if lat is None or lon is None:
            continue
        lines.append(f'      <trkpt lat="{lat}" lon="{lon}">')
        iso_ts = _format_timestamp(pt.get('time') or pt.get('timestamp'), start_dt)
        if 'ele' in pt and pt['ele'] is not None:
            lines.append(f'        <ele>{pt["ele"]}</ele>')
        if iso_ts:
            lines.append(f'        <time>{iso_ts}</time>')
        unc = pt.get('uncertainty_m')
        if unc is not None:
            lines.append(f'        <extensions><uncertainty>{unc:.2f}</uncertainty></extensions>')
        lines.append('      </trkpt>')
    lines.append('    </trkseg>')
    lines.append('  </trk>')


def _convert_points(points):
    converted = []
    for pt in points or []:
        lat = pt.get('lat')
        lon = pt.get('lon')
        if lat is None or lon is None:
            continue
        converted.append({
            'lat': lat,
            'lon': lon,
            'timestamp': pt.get('timestamp'),
            'uncertainty_m': pt.get('uncertainty_m')
        })
    return converted


def _maybe_replay_trajectories(data):
    trajectories = data.get('trajectories') or {}
    if replay_mod is None:
        return trajectories

    has_dense = any(len(trajectories.get(key, [])) >= 20 for key in ('ekf', 'complementary', 'es_ekf'))
    if has_dense:
        return trajectories

    try:
        events, start_offset = replay_mod.build_events(data)
        if not events:
            return trajectories
        include_es = True
        replayed = replay_mod.replay_session(data, start_timestamp=-start_offset, include_es=include_es)
    except Exception as exc:
        print(f"Replay fallback failed: {exc}")
        return trajectories

    result = dict(trajectories)
    result['ekf'] = _convert_points(replayed.get('ekf'))
    result['complementary'] = _convert_points(replayed.get('complementary'))
    if 'es_ekf' in replayed:
        result['es_ekf'] = _convert_points(replayed.get('es_ekf'))
    if 'gps' in replayed and not result.get('gps'):
        result['gps'] = _convert_points(replayed.get('gps'))
    return result


def generate_gpx(json_filepath: str, output_filepath: str = None):
    """Generate multi-track GPX from comparison JSON file"""

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

    trajectories = _maybe_replay_trajectories(data)
    if not gps_points and trajectories.get('gps'):
        gps_points = trajectories['gps']
    ekf_track = trajectories.get('ekf', [])
    comp_track = trajectories.get('complementary', [])
    es_track = trajectories.get('es_ekf', [])

    if not gps_points and not ekf_track and not comp_track:
        print("ERROR: No trajectory data found!")
        return

    start_dt = datetime.now(timezone.utc)

    gpx_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="Motion Tracker Dashboard" xmlns="http://www.topografix.com/GPX/1/1">',
        '  <metadata>',
        f'    <time>{start_dt.isoformat()}Z</time>',
        '    <desc>GPS + filtered trajectories</desc>',
        '  </metadata>'
    ]

    _append_track(gpx_lines, 'ES-EKF', 'Error-state EKF trajectory (smoothed path)', es_track, start_dt)
    _append_track(gpx_lines, 'EKF', 'Extended Kalman Filter trajectory', ekf_track, start_dt)
    _append_track(gpx_lines, 'GPS', 'Raw GPS fixes', gps_points, start_dt)
    _append_track(gpx_lines, 'Complementary', 'Complementary filter trajectory', comp_track, start_dt)

    gpx_lines.append('</gpx>')
    gpx = '\n'.join(gpx_lines) + '\n'

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
