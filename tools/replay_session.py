#!/usr/bin/env python3
"""
Replay a saved comparison session to regenerate EKF/Complementary trajectories
and export a multi-track GPX (GPS + filtered tracks).

Useful for older runs that only stored GPS fixes. Refeeds the recorded samples
through the filters with deterministic timing so we can visualize the EKF path
without collecting new sensor data.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
# Ensure repository root is importable when running as a standalone script
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import motion_tracker_v2.filters.complementary as complementary_mod
import motion_tracker_v2.filters.ekf as ekf_mod
from motion_tracker_v2.filters.complementary import ComplementaryFilter
from motion_tracker_v2.filters.ekf import ExtendedKalmanFilter

try:
    import motion_tracker_v2.filters.es_ekf as es_ekf_mod
    from motion_tracker_v2.filters.es_ekf import ErrorStateEKF
    HAS_ES_EKF = True
except ImportError:
    HAS_ES_EKF = False


@dataclass
class ReplayEvent:
    timestamp: float
    kind: str
    payload: Dict


class ReplayClock:
    """Simple clock override so filters use recorded timestamps instead of wall time."""

    def __init__(self) -> None:
        self._value = 0.0

    def set(self, value: float) -> None:
        self._value = value

    def now(self) -> float:
        return self._value


def load_session(path: Path) -> Dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def build_events(data: Dict) -> Tuple[List[ReplayEvent], float]:
    events: List[ReplayEvent] = []

    def add_events(samples: Iterable[Dict], kind: str) -> None:
        for sample in samples or []:
            ts = sample.get("timestamp")
            if ts is None:
                ts = sample.get("elapsed")
            if ts is None:
                continue
            events.append(ReplayEvent(float(ts), kind, sample))

    add_events(data.get("accel_samples", []), "accel")
    add_events(data.get("gps_samples", []), "gps")
    add_events(data.get("gyro_samples", []), "gyro")

    if not events:
        raise RuntimeError("Session has no samples to replay")

    events.sort(key=lambda ev: ev.timestamp)
    start_ts = events[0].timestamp
    return events, start_ts


def patch_time(fake_clock: ReplayClock, include_es: bool) -> List[Tuple[object, str, object]]:
    """Monkey-patch time.time inside filter modules so they use replay timestamps."""
    patches: List[Tuple[object, str, object]] = []

    def patch(module: object) -> None:
        if hasattr(module.time, "time"):
            patches.append((module.time, "time", module.time.time))
            module.time.time = fake_clock.now  # type: ignore[attr-defined]

    patch(complementary_mod)
    patch(ekf_mod)
    if include_es and HAS_ES_EKF:
        patch(es_ekf_mod)
    return patches


def restore_time(patches: List[Tuple[object, str, object]]) -> None:
    for obj, attr, original in patches:
        setattr(obj, attr, original)


def record_track(
    track: List[Dict],
    ts: float,
    lat: Optional[float],
    lon: Optional[float],
    uncertainty: Optional[float],
) -> None:
    if lat is None or lon is None or math.isinf(lat) or math.isinf(lon):
        return
    entry = {"timestamp": ts, "lat": lat, "lon": lon}
    if uncertainty is not None:
        entry["uncertainty_m"] = uncertainty
    track.append(entry)


def replay_session(
    data: Dict,
    start_timestamp: float,
    include_es: bool = False,
) -> Dict[str, List[Dict]]:
    fake_clock = ReplayClock()
    patches = patch_time(fake_clock, include_es)

    try:
        ekf = ExtendedKalmanFilter(enable_gyro=include_es)
        comp = ComplementaryFilter()
        es_ekf: Optional[ErrorStateEKF] = None
        if include_es and HAS_ES_EKF:
            es_ekf = ErrorStateEKF(enable_gyro=include_es)

        tracks = {
            "gps": [],
            "ekf": [],
            "complementary": [],
        }
        if include_es and HAS_ES_EKF:
            tracks["es_ekf"] = []

        events, _ = build_events(data)

        for event in events:
            fake_clock.set(start_timestamp + event.timestamp)
            if event.kind == "accel":
                magnitude = float(event.payload.get("magnitude", 0.0))
                comp.update_accelerometer(magnitude)
                ekf.update_accelerometer(magnitude)
                if es_ekf:
                    es_ekf.update_accelerometer(magnitude)
            elif event.kind == "gps":
                lat = event.payload.get("latitude")
                lon = event.payload.get("longitude")
                speed = event.payload.get("speed")
                accuracy = event.payload.get("accuracy")
                comp.update_gps(lat, lon, speed, accuracy)
                ekf.update_gps(lat, lon, speed, accuracy)
                if es_ekf:
                    es_ekf.update_gps(lat, lon, speed, accuracy)
                record_track(
                    tracks["gps"],
                    event.timestamp,
                    lat,
                    lon,
                    accuracy,
                )
            elif event.kind == "gyro" and es_ekf:
                es_ekf.update_gyroscope(
                    event.payload.get("x", 0.0),
                    event.payload.get("y", 0.0),
                    event.payload.get("z", 0.0),
                )

            lat, lon, unc = ekf.get_position()
            record_track(tracks["ekf"], event.timestamp, lat, lon, unc)
            c_lat, c_lon, c_unc = comp.get_position()
            record_track(tracks["complementary"], event.timestamp, c_lat, c_lon, c_unc)
            if es_ekf:
                es_lat, es_lon, es_unc = es_ekf.get_position()
                record_track(tracks["es_ekf"], event.timestamp, es_lat, es_lon, es_unc)

        return tracks
    finally:
        restore_time(patches)


def write_gpx(
    tracks: Dict[str, List[Dict]],
    output_path: Path,
    start_dt: datetime,
) -> None:
    def iso(ts: float) -> str:
        return (start_dt + timedelta(seconds=ts)).isoformat() + "Z"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="gojo-replay" xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <metadata>",
        f"    <time>{start_dt.isoformat()}Z</time>",
        f"    <desc>Replayed track with GPS + filtered trajectories</desc>",
        f"  </metadata>",
    ]

    def write_track(name: str, desc: str, points: List[Dict]) -> None:
        if not points:
            return
        lines.append("  <trk>")
        lines.append(f"    <name>{name}</name>")
        lines.append(f"    <desc>{desc}</desc>")
        lines.append("    <trkseg>")
        for pt in points:
            lines.append(f'      <trkpt lat="{pt["lat"]}" lon="{pt["lon"]}">')
            lines.append(f"        <time>{iso(pt['timestamp'])}</time>")
            if "uncertainty_m" in pt:
                lines.append(
                    f'        <extensions><uncertainty>{pt["uncertainty_m"]:.2f}</uncertainty></extensions>'
                )
            lines.append("      </trkpt>")
        lines.append("    </trkseg>")
        lines.append("  </trk>")

    write_track("ES-EKF", "Error-state EKF trajectory", tracks.get("es_ekf", []))
    write_track("EKF", "Extended Kalman Filter trajectory", tracks.get("ekf", []))
    write_track("GPS", "Raw GPS fixes", tracks.get("gps", []))
    write_track(
        "Complementary",
        "Complementary filter (GPS-weighted fusion)",
        tracks.get("complementary", []),
    )

    lines.append("</gpx>")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "session",
        type=Path,
        help="Path to comparison_*.json[.gz] session file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional GPX output path (defaults to comparison_*_replay.gpx)",
    )
    parser.add_argument(
        "--start-time",
        help="ISO8601 timestamp to use for GPX metadata (default: current UTC)",
    )
    parser.set_defaults(include_es_ekf=True)
    parser.add_argument(
        "--include-es-ekf",
        dest="include_es_ekf",
        action="store_true",
        help="(default) replay the ES-EKF track if data and module are available",
    )
    parser.add_argument(
        "--no-es-ekf",
        dest="include_es_ekf",
        action="store_false",
        help="Disable ES-EKF replay (only GPS/EKF/Complementary)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = load_session(args.session)
    events, min_ts = build_events(data)

    include_es = args.include_es_ekf and HAS_ES_EKF
    if args.include_es_ekf and not HAS_ES_EKF:
        print("⚠ ES-EKF module unavailable; skipping ES-EKF track")

    base_time = events[0].timestamp
    tracks = replay_session(data, start_timestamp=-base_time, include_es=include_es)

    # Determine start time for GPX metadata
    if args.start_time:
        start_dt = datetime.fromisoformat(args.start_time)
    else:
        start_dt = datetime.now(timezone.utc)

    output = (
        args.output
        if args.output
        else args.session.with_suffix("").with_name(args.session.stem + "_replay.gpx")
    )

    write_gpx(tracks, output, start_dt)
    print(f"✓ Replayed session saved to {output}")


if __name__ == "__main__":
    main()
