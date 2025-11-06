#!/usr/bin/env python3
"""
Motion Tracker V2 - Browser Dashboard Server
FastAPI server for viewing drives with Leaflet.js map integration
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import json
import gzip
import os
from datetime import datetime
from pathlib import Path
import math

app = FastAPI(title="Motion Tracker Dashboard")

# Configuration
BASE_DIR = os.path.expanduser("~/gojo")
SESSIONS_DIR = os.path.join(BASE_DIR, "motion_tracker_sessions")
SESSIONS_SUBDIR = os.path.join(BASE_DIR, "sessions")  # Also check here for motion_tracker_v2 runs
os.makedirs(SESSIONS_DIR, exist_ok=True)


def parse_timestamp(filename: str) -> datetime:
    """Parse timestamp from filename (handles both motion_track_v2 and comparison formats)"""
    try:
        # Remove extensions first
        base = filename.replace(".json.gz", "").replace(".json", "").replace(".gpx", "")

        # Extract YYYYMMDD_HHMMSS pattern (last 15 chars after removing prefix)
        # Works for: motion_track_v2_20251104_121001 and comparison_20251104_121001
        parts = base.split("_")
        if len(parts) >= 2:
            # Try to find YYYYMMDD and HHMMSS in the parts
            for i in range(len(parts) - 1):
                if len(parts[i]) == 8 and parts[i].isdigit():  # YYYYMMDD
                    if len(parts[i+1]) >= 6 and parts[i+1][:6].isdigit():  # HHMMSS
                        timestamp_str = f"{parts[i]}_{parts[i+1][:6]}"
                        return datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")

        # Fallback: try last two parts
        timestamp_str = f"{parts[-2]}_{parts[-1]}"
        return datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
    except Exception as e:
        print(f"Timestamp parse failed for {filename}: {e}")
        return datetime.now()


def load_json_file(filepath: str) -> dict:
    """Load JSON file, handling both .json and .json.gz formats"""
    try:
        if filepath.endswith(".gz"):
            with gzip.open(filepath, "rt") as f:
                return json.load(f)
        else:
            with open(filepath, "r") as f:
                return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to load file: {str(e)}")


def load_gpx_file(filepath: str) -> str:
    """Load GPX file content"""
    try:
        if filepath.endswith(".gz"):
            with gzip.open(filepath, "rt") as f:
                return f.read()
        else:
            with open(filepath, "r") as f:
                return f.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to load GPX: {str(e)}")


def generate_gpx_from_json(json_filepath: str) -> str:
    """Generate GPX from JSON GPS data (for test_ekf comparison files)"""
    try:
        data = load_json_file(json_filepath)

        # Try to extract GPS data from various formats
        gps_points = []

        # Format 1: motion_track_v2 with gps_data array
        if "gps_data" in data and isinstance(data["gps_data"], list):
            for sample in data["gps_data"]:
                if "gps" in sample and isinstance(sample["gps"], dict):
                    gps = sample["gps"]
                    if "latitude" in gps and "longitude" in gps:
                        gps_points.append({
                            "lat": gps["latitude"],
                            "lon": gps["longitude"],
                            "ele": gps.get("altitude", 0),
                            "time": sample.get("timestamp", "")
                        })

        # Format 2: motion_track_v2 with gps_samples array (nested format)
        elif "gps_samples" in data and isinstance(data["gps_samples"], list):
            for sample in data["gps_samples"]:
                if isinstance(sample, dict):
                    # Check nested format first (sample["gps"]["latitude"])
                    if "gps" in sample:
                        gps = sample["gps"]
                        if isinstance(gps, dict) and "latitude" in gps and "longitude" in gps:
                            gps_points.append({
                                "lat": gps["latitude"],
                                "lon": gps["longitude"],
                                "ele": gps.get("altitude", 0),
                                "time": sample.get("timestamp", "")
                            })
                    # Check flat format (sample["latitude"]) - comparison files use this
                    elif "latitude" in sample and "longitude" in sample:
                        gps_points.append({
                            "lat": sample["latitude"],
                            "lon": sample["longitude"],
                            "ele": sample.get("altitude", 0),
                            "time": sample.get("timestamp", "")
                        })

        if not gps_points:
            raise ValueError("No GPS data found in JSON file")

        # Generate GPX (without namespace to ensure JavaScript parsing works)
        gpx = '<?xml version="1.0" encoding="UTF-8"?>\n'
        gpx += '<gpx version="1.1" creator="Motion Tracker Dashboard">\n'
        gpx += '  <metadata>\n'
        gpx += f'    <time>{datetime.now().isoformat()}Z</time>\n'
        gpx += '  </metadata>\n'
        gpx += '  <trk>\n'
        gpx += '    <name>Drive Track</name>\n'
        gpx += '    <trkseg>\n'

        for point in gps_points:
            gpx += f'      <trkpt lat="{point["lat"]}" lon="{point["lon"]}">\n'
            if point["ele"]:
                gpx += f'        <ele>{point["ele"]}</ele>\n'
            if point["time"]:
                gpx += f'        <time>{point["time"]}Z</time>\n'
            gpx += '      </trkpt>\n'

        gpx += '    </trkseg>\n'
        gpx += '  </trk>\n'
        gpx += '</gpx>\n'

        return gpx
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to generate GPX: {str(e)}")


def get_drive_stats(data: dict) -> dict:
    """Extract stats from drive data (handles multiple formats)"""
    stats = {
        "gps_samples": 0,
        "accel_samples": 0,
        "gyro_samples": 0,
        "distance_km": 0,
        "peak_memory_mb": 0,
    }

    # Extract sample counts (handle both int and list formats)
    for field in ["gps_samples", "accel_samples", "gyro_samples"]:
        if isinstance(data.get(field), int):
            stats[field] = data[field]
        elif isinstance(data.get(field), list):
            stats[field] = len(data[field])

    # Extract peak memory
    if isinstance(data.get("peak_memory_mb"), (int, float)):
        stats["peak_memory_mb"] = data["peak_memory_mb"]

    # Extract distance - check in priority order
    # 1. Nested final_metrics (comparison test files - check first!)
    if "final_metrics" in data:
        metrics = data["final_metrics"]
        if isinstance(metrics, dict):
            # Try nested filter metrics (comparison format)
            if "ekf" in metrics and isinstance(metrics["ekf"], dict) and "distance" in metrics["ekf"]:
                dist = metrics["ekf"]["distance"]
                if isinstance(dist, (int, float)):
                    stats["distance_km"] = round(dist / 1000, 2)
            elif "complementary" in metrics and isinstance(metrics["complementary"], dict) and "distance" in metrics["complementary"]:
                dist = metrics["complementary"]["distance"]
                if isinstance(dist, (int, float)):
                    stats["distance_km"] = round(dist / 1000, 2)
            # Try top-level metrics (other formats)
            elif "distance_m" in metrics:
                dist = metrics["distance_m"]
                if isinstance(dist, (int, float)):
                    stats["distance_km"] = round(dist / 1000, 2)
            elif "distance_km" in metrics:
                dist = metrics["distance_km"]
                if isinstance(dist, (int, float)):
                    stats["distance_km"] = round(dist, 2)

    # 2. Top-level total_distance (actual drive files)
    elif "total_distance" in data and isinstance(data["total_distance"], (int, float)):
        stats["distance_km"] = round(data["total_distance"] / 1000, 2)

    # 3. Last GPS sample distance (actual drive files with gps_samples array)
    elif "gps_samples" in data and isinstance(data["gps_samples"], list) and len(data["gps_samples"]) > 0:
        try:
            last_gps = data["gps_samples"][-1]
            if isinstance(last_gps, dict) and "distance" in last_gps and isinstance(last_gps["distance"], (int, float)):
                stats["distance_km"] = round(last_gps["distance"] / 1000, 2)
        except Exception as e:
            print(f"GPS array distance extraction failed: {e}")

    # Sanity check
    if stats["distance_km"] < 0 or stats["distance_km"] > 10000:
        print(f"Warning: Suspicious distance value: {stats['distance_km']} km")
        stats["distance_km"] = 0

    return stats


@app.get("/api/drives")
def list_drives():
    """List all available drives from motion_tracker_v2 sessions"""
    drives = []

    # Search in sessions subdirectories (motion_tracker_v2 output)
    if os.path.exists(SESSIONS_SUBDIR):
        for session_dir in os.listdir(SESSIONS_SUBDIR):
            session_path = os.path.join(SESSIONS_SUBDIR, session_dir)
            if not os.path.isdir(session_path):
                continue

            for filename in sorted(os.listdir(session_path)):
                if filename.startswith("motion_track_v2_") and filename.endswith(".json"):
                    filepath = os.path.join(session_path, filename)
                    gpx_filepath = filepath.replace(".json", ".gpx")

                    try:
                        data = load_json_file(filepath)
                        stats = get_drive_stats(data)
                        timestamp = parse_timestamp(filename)

                        drives.append({
                            "id": filename.replace(".json", ""),
                            "path": filepath,
                            "gpx_path": gpx_filepath,
                            "timestamp": timestamp.isoformat(),
                            "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                            "has_gpx": os.path.exists(gpx_filepath),
                            "file_size_mb": round(os.path.getsize(filepath) / (1024 * 1024), 2),
                            "stats": stats,
                        })
                    except Exception as e:
                        print(f"Error loading {filename}: {e}")
                        continue

    # Also search in main motion_tracker_sessions directory (for comparison runs)
    if os.path.exists(SESSIONS_DIR):
        for filename in sorted(os.listdir(SESSIONS_DIR)):
            # Include both motion_track_v2 (actual drives) and comparison (test results) files
            if (filename.startswith("motion_track_v2_") or filename.startswith("comparison_")) and filename.endswith(".json"):
                filepath = os.path.join(SESSIONS_DIR, filename)
                gpx_filepath = filepath.replace(".json", ".gpx")

                try:
                    data = load_json_file(filepath)
                    stats = get_drive_stats(data)
                    timestamp = parse_timestamp(filename)

                    # Check if GPX exists or can be generated from JSON
                    has_gpx = os.path.exists(gpx_filepath)
                    if not has_gpx:
                        # Check if JSON has actual GPS points (with lat/lon) that can be converted
                        try:
                            # motion_track_v2 format with gps_data containing actual GPS coordinates
                            if "gps_data" in data and isinstance(data["gps_data"], list) and len(data["gps_data"]) > 0:
                                for sample in data["gps_data"]:
                                    if "gps" in sample and isinstance(sample["gps"], dict):
                                        if "latitude" in sample["gps"] and "longitude" in sample["gps"]:
                                            has_gpx = True
                                            break
                            # motion_track_v2 format with gps_samples array
                            elif "gps_samples" in data and isinstance(data["gps_samples"], list) and len(data["gps_samples"]) > 0:
                                for sample in data["gps_samples"]:
                                    if isinstance(sample, dict):
                                        # Check nested format (sample["gps"]["latitude"])
                                        if "gps" in sample and isinstance(sample["gps"], dict):
                                            if "latitude" in sample["gps"] and "longitude" in sample["gps"]:
                                                has_gpx = True
                                                break
                                        # Check flat format (sample["latitude"]) - comparison files use this
                                        elif "latitude" in sample and "longitude" in sample:
                                            has_gpx = True
                                            break
                        except:
                            pass

                    drives.append({
                        "id": filename.replace(".json", ""),
                        "path": filepath,
                        "gpx_path": gpx_filepath,
                        "timestamp": timestamp.isoformat(),
                        "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "has_gpx": has_gpx,
                        "file_size_mb": round(os.path.getsize(filepath) / (1024 * 1024), 2),
                        "stats": stats,
                    })
                except Exception as e:
                    print(f"Error loading {filename}: {e}")
                    continue

    return {"drives": sorted(drives, key=lambda x: x["timestamp"], reverse=True)}


@app.get("/api/drive/{drive_id}")
def get_drive_details(drive_id: str):
    """Get detailed data for a specific drive"""
    # Find the JSON file in either location
    json_filepath = None
    gpx_filepath = None

    # Search in sessions subdirectories
    if os.path.exists(SESSIONS_SUBDIR):
        for session_dir in os.listdir(SESSIONS_SUBDIR):
            session_path = os.path.join(SESSIONS_SUBDIR, session_dir)
            if os.path.isdir(session_path):
                for filename in os.listdir(session_path):
                    if filename.startswith(drive_id) and filename.endswith(".json"):
                        json_filepath = os.path.join(session_path, filename)
                        gpx_filepath = json_filepath.replace(".json", ".gpx")
                        break
            if json_filepath:
                break

    # Search in main sessions directory if not found
    if not json_filepath and os.path.exists(SESSIONS_DIR):
        for filename in os.listdir(SESSIONS_DIR):
            if filename.startswith(drive_id) and filename.endswith(".json"):
                json_filepath = os.path.join(SESSIONS_DIR, filename)
                gpx_filepath = json_filepath.replace(".json", ".gpx")
                break

    if not json_filepath:
        raise HTTPException(status_code=404, detail="Drive not found")

    try:
        data = load_json_file(json_filepath)
        stats = get_drive_stats(data)
        timestamp = parse_timestamp(os.path.basename(json_filepath))

        # Check if GPX exists or can be generated from JSON (same logic as /api/drives)
        has_gpx = os.path.exists(gpx_filepath)
        if not has_gpx:
            # Check if JSON has actual GPS points (with lat/lon) that can be converted
            try:
                # motion_track_v2 format with gps_data containing actual GPS coordinates
                if "gps_data" in data and isinstance(data["gps_data"], list) and len(data["gps_data"]) > 0:
                    for sample in data["gps_data"]:
                        if "gps" in sample and isinstance(sample["gps"], dict):
                            if "latitude" in sample["gps"] and "longitude" in sample["gps"]:
                                has_gpx = True
                                break
                # motion_track_v2 format with gps_samples array
                elif "gps_samples" in data and isinstance(data["gps_samples"], list) and len(data["gps_samples"]) > 0:
                    for sample in data["gps_samples"]:
                        if isinstance(sample, dict):
                            # Check nested format (sample["gps"]["latitude"])
                            if "gps" in sample and isinstance(sample["gps"], dict):
                                if "latitude" in sample["gps"] and "longitude" in sample["gps"]:
                                    has_gpx = True
                                    break
                            # Check flat format (sample["latitude"]) - comparison files use this
                            elif "latitude" in sample and "longitude" in sample:
                                has_gpx = True
                                break
            except:
                pass

        return {
            "id": drive_id,
            "timestamp": timestamp.isoformat(),
            "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "has_gpx": has_gpx,
            "stats": stats,
            "metadata": data.get("final_metrics", {}),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/drive/{drive_id}/gpx")
def get_drive_gpx(drive_id: str):
    """Get GPX file for a specific drive (load from file or generate from JSON)"""
    # Find the GPX file first
    gpx_filepath = None
    json_filepath = None

    # Search in sessions subdirectories for GPX
    if not gpx_filepath and not json_filepath and os.path.exists(SESSIONS_SUBDIR):
        for session_dir in os.listdir(SESSIONS_SUBDIR):
            session_path = os.path.join(SESSIONS_SUBDIR, session_dir)
            if os.path.isdir(session_path):
                for filename in os.listdir(session_path):
                    if filename.startswith(drive_id):
                        if filename.endswith(".gpx"):
                            gpx_filepath = os.path.join(session_path, filename)
                        elif filename.endswith(".json"):
                            json_filepath = os.path.join(session_path, filename)
            if gpx_filepath or json_filepath:
                break

    # Search in main sessions directory if not found
    if not gpx_filepath and not json_filepath and os.path.exists(SESSIONS_DIR):
        for filename in os.listdir(SESSIONS_DIR):
            if filename.startswith(drive_id):
                if filename.endswith(".gpx"):
                    gpx_filepath = os.path.join(SESSIONS_DIR, filename)
                elif filename.endswith(".json"):
                    json_filepath = os.path.join(SESSIONS_DIR, filename)
            if gpx_filepath or json_filepath:
                break

    # Load existing GPX if found
    if gpx_filepath and os.path.exists(gpx_filepath):
        try:
            gpx_content = load_gpx_file(gpx_filepath)
            return HTMLResponse(content=gpx_content, media_type="application/gpx+xml")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Generate GPX from JSON if available
    if json_filepath and os.path.exists(json_filepath):
        try:
            gpx_content = generate_gpx_from_json(json_filepath)
            return HTMLResponse(content=gpx_content, media_type="application/gpx+xml")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    raise HTTPException(status_code=404, detail="No GPX or GPS data found for this drive")


@app.get("/api/live/status")
def get_live_status():
    """Get live tracking session status (file-based IPC)"""
    status_file = os.path.join(SESSIONS_DIR, 'live_status.json')

    try:
        if not os.path.exists(status_file):
            return {"status": "INACTIVE", "message": "No active tracking session"}

        # Check if status file is stale (>10 seconds old = session likely crashed)
        file_age = datetime.now().timestamp() - os.path.getmtime(status_file)
        if file_age > 10:
            return {"status": "STALE", "message": f"Session inactive for {int(file_age)}s", "file_age": file_age}

        with open(status_file, 'r') as f:
            status_data = json.load(f)

        return status_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read live status: {str(e)}")


@app.get("/api/live/data/{session_id}")
def get_live_data(session_id: str):
    """Get latest auto-saved data for active session"""
    # Look for the session's latest auto-save file
    json_filepath = os.path.join(SESSIONS_DIR, f"{session_id}.json.gz")

    if not os.path.exists(json_filepath):
        # Try uncompressed
        json_filepath = os.path.join(SESSIONS_DIR, f"{session_id}.json")

    if not os.path.exists(json_filepath):
        raise HTTPException(status_code=404, detail="Session data not found (no auto-save yet)")

    try:
        data = load_json_file(json_filepath)

        # Extract latest GPS samples for route display (last 100 points)
        gps_samples = []
        if "gps_samples" in data and isinstance(data["gps_samples"], list):
            gps_samples = data["gps_samples"][-100:]  # Last 100 GPS points

        return {
            "gps_samples": gps_samples,
            "total_gps": len(data.get("gps_samples", [])),
            "total_accel": len(data.get("accel_samples", [])),
            "total_gyro": len(data.get("gyro_samples", [])),
            "auto_save": data.get("auto_save", False),
            "autosave_number": data.get("autosave_number", 0),
            "peak_memory_mb": data.get("peak_memory_mb", 0)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to load session data: {str(e)}")


@app.get("/live")
def live_monitor():
    """Serve the live drive monitor page"""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Drive Monitor</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: #1a1a1a;
            color: #fff;
            height: 100vh;
            overflow: hidden;
        }

        .container {
            display: flex;
            height: 100vh;
        }

        .sidebar {
            width: 320px;
            background: #2a2a2a;
            border-right: 1px solid #444;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
        }

        .header {
            padding: 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }

        .header h1 {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 5px;
        }

        .header p {
            font-size: 13px;
            opacity: 0.9;
        }

        .status-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            margin-top: 10px;
        }

        .status-active {
            background: #4caf50;
            color: white;
        }

        .status-inactive {
            background: #f44336;
            color: white;
        }

        .status-stale {
            background: #ff9800;
            color: white;
        }

        .metrics-panel {
            padding: 20px;
            flex: 1;
        }

        .metric-group {
            background: #333;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 15px;
        }

        .metric-group h3 {
            font-size: 12px;
            color: #999;
            text-transform: uppercase;
            margin-bottom: 10px;
            letter-spacing: 0.5px;
        }

        .metric-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #444;
        }

        .metric-row:last-child {
            border-bottom: none;
        }

        .metric-label {
            font-size: 13px;
            color: #bbb;
        }

        .metric-value {
            font-size: 14px;
            font-weight: 600;
            color: #fff;
        }

        .metric-value.highlight {
            color: #667eea;
            font-size: 20px;
        }

        .incidents-list {
            max-height: 150px;
            overflow-y: auto;
            margin-top: 10px;
        }

        .incident-item {
            background: #444;
            padding: 8px;
            margin: 5px 0;
            border-radius: 4px;
            font-size: 12px;
        }

        .incident-swerving { border-left: 3px solid #ff9800; }
        .incident-braking { border-left: 3px solid #f44336; }
        .incident-impact { border-left: 3px solid #9c27b0; }

        .map-container {
            flex: 1;
            position: relative;
        }

        #map {
            width: 100%;
            height: 100%;
        }

        .map-overlay {
            position: absolute;
            top: 20px;
            right: 20px;
            background: rgba(255, 255, 255, 0.95);
            padding: 15px 20px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
            z-index: 1000;
            min-width: 200px;
        }

        .map-overlay h3 {
            font-size: 13px;
            color: #667eea;
            margin-bottom: 10px;
            text-transform: uppercase;
        }

        .map-overlay .metric {
            display: flex;
            justify-content: space-between;
            margin: 5px 0;
            font-size: 13px;
            color: #333;
        }

        .loading {
            text-align: center;
            padding: 40px 20px;
            color: #999;
        }

        .loading-spinner {
            width: 40px;
            height: 40px;
            margin: 0 auto 10px;
            border: 3px solid #444;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .error {
            color: #f44336;
            padding: 20px;
            background: #3a1a1a;
            border-left: 4px solid #f44336;
            margin: 20px;
            border-radius: 4px;
            font-size: 13px;
        }

        .nav-button {
            position: absolute;
            top: 20px;
            left: 20px;
            background: white;
            padding: 10px 15px;
            border-radius: 6px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.2);
            z-index: 1000;
            text-decoration: none;
            color: #667eea;
            font-weight: 600;
            font-size: 13px;
        }

        .nav-button:hover {
            background: #f5f5f5;
        }

        @media (max-width: 768px) {
            .container {
                flex-direction: column;
            }

            .sidebar {
                width: 100%;
                height: 50vh;
                border-right: none;
                border-bottom: 1px solid #444;
            }

            .map-container {
                height: 50vh;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="sidebar">
            <div class="header">
                <h1>Live Drive Monitor</h1>
                <p id="sessionInfo">Waiting for session...</p>
                <div id="statusBadge" class="status-badge status-inactive">INACTIVE</div>
            </div>

            <div class="metrics-panel">
                <div class="metric-group">
                    <h3>Current State</h3>
                    <div class="metric-row">
                        <span class="metric-label">Velocity</span>
                        <span class="metric-value highlight" id="velocity">0.0 m/s</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Distance</span>
                        <span class="metric-value" id="distance">0.0 km</span>
                    </div>
                    <div class="metric-row" id="headingRow" style="display:none;">
                        <span class="metric-label">Heading</span>
                        <span class="metric-value" id="heading">0°</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Duration</span>
                        <span class="metric-value" id="duration">00:00</span>
                    </div>
                </div>

                <div class="metric-group">
                    <h3>Sensors</h3>
                    <div class="metric-row">
                        <span class="metric-label">GPS Fixes</span>
                        <span class="metric-value" id="gpsFixes">0</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Accel Samples</span>
                        <span class="metric-value" id="accelSamples">0</span>
                    </div>
                    <div class="metric-row" id="gyroRow" style="display:none;">
                        <span class="metric-label">Gyro Samples</span>
                        <span class="metric-value" id="gyroSamples">0</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Memory</span>
                        <span class="metric-value" id="memory">0 MB</span>
                    </div>
                </div>

                <div class="metric-group">
                    <h3>Incidents</h3>
                    <div class="metric-row">
                        <span class="metric-label">Total Events</span>
                        <span class="metric-value" id="incidentCount">0</span>
                    </div>
                    <div id="incidentsList" class="incidents-list"></div>
                </div>
            </div>
        </div>

        <div class="map-container">
            <a href="/" class="nav-button">← Back to Drives</a>
            <div id="map"></div>
            <div class="map-overlay">
                <h3>Live Position</h3>
                <div id="mapMetrics">
                    <div class="metric">
                        <span>Speed:</span>
                        <span id="mapSpeed">0 km/h</span>
                    </div>
                    <div class="metric">
                        <span>GPS Accuracy:</span>
                        <span id="mapAccuracy">-</span>
                    </div>
                    <div class="metric">
                        <span>Last Update:</span>
                        <span id="mapLastUpdate">-</span>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let map = null;
        let currentMarker = null;
        let routePolyline = null;
        let allRoutePoints = [];
        let currentSessionId = null;
        let pollInterval = null;
        let lastUpdateTime = 0;

        // Initialize map
        function initMap() {
            map = L.map('map').setView([37.7749, -122.4194], 13);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '© OpenStreetMap contributors',
                maxZoom: 19,
            }).addTo(map);

            // Create route polyline (empty initially)
            routePolyline = L.polyline([], {
                color: '#667eea',
                weight: 3,
                opacity: 0.8,
            }).addTo(map);

            // Create current position marker
            currentMarker = L.circleMarker([37.7749, -122.4194], {
                radius: 8,
                fillColor: '#4caf50',
                color: '#fff',
                weight: 2,
                opacity: 1,
                fillOpacity: 0.9,
            }).addTo(map);
        }

        // Format duration (seconds to MM:SS)
        function formatDuration(seconds) {
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
        }

        // Poll for live status
        async function pollLiveStatus() {
            try {
                const response = await fetch('/api/live/status');
                const status = await response.json();

                if (status.status === 'ACTIVE') {
                    updateDashboard(status);
                    if (currentSessionId !== status.session_id) {
                        currentSessionId = status.session_id;
                        allRoutePoints = [];
                        console.log('New session started:', currentSessionId);
                    }
                    await fetchRouteData(status.session_id);
                } else if (status.status === 'INACTIVE') {
                    showInactive();
                } else if (status.status === 'STALE') {
                    showStale(status.file_age);
                }

            } catch (error) {
                console.error('Error polling live status:', error);
                showError('Connection error');
            }
        }

        // Update dashboard with live data
        function updateDashboard(status) {
            document.getElementById('statusBadge').className = 'status-badge status-active';
            document.getElementById('statusBadge').textContent = 'LIVE';
            document.getElementById('sessionInfo').textContent = `Session: ${status.session_id}`;

            // Metrics
            document.getElementById('velocity').textContent = `${status.current_velocity.toFixed(1)} m/s`;
            document.getElementById('distance').textContent = `${(status.total_distance / 1000).toFixed(2)} km`;
            document.getElementById('duration').textContent = formatDuration(status.elapsed_seconds);
            document.getElementById('gpsFixes').textContent = status.gps_fixes;
            document.getElementById('accelSamples').textContent = status.accel_samples;
            document.getElementById('memory').textContent = `${status.memory_mb} MB`;
            document.getElementById('incidentCount').textContent = status.incidents_count;

            // Heading (if available)
            if (status.current_heading !== null) {
                document.getElementById('headingRow').style.display = 'flex';
                document.getElementById('heading').textContent = `${status.current_heading}°`;
            }

            // Gyro (if available)
            if (status.gyro_samples > 0) {
                document.getElementById('gyroRow').style.display = 'flex';
                document.getElementById('gyroSamples').textContent = status.gyro_samples;
            }

            // Map overlay
            const speedKmh = status.current_velocity * 3.6;
            document.getElementById('mapSpeed').textContent = `${speedKmh.toFixed(1)} km/h`;

            if (status.latest_gps) {
                document.getElementById('mapAccuracy').textContent = `${status.latest_gps.accuracy.toFixed(0)} m`;
            }

            const now = Date.now() / 1000;
            const timeSinceUpdate = Math.floor(now - status.last_update);
            document.getElementById('mapLastUpdate').textContent = `${timeSinceUpdate}s ago`;

            // Update current position marker
            if (status.latest_gps) {
                const lat = status.latest_gps.lat;
                const lon = status.latest_gps.lon;
                currentMarker.setLatLng([lat, lon]);
                map.setView([lat, lon], map.getZoom() > 15 ? map.getZoom() : 16);
            }

            lastUpdateTime = status.last_update;
        }

        // Fetch route data from auto-save
        async function fetchRouteData(sessionId) {
            try {
                const response = await fetch(`/api/live/data/${sessionId}`);
                const data = await response.json();

                if (data.gps_samples && data.gps_samples.length > 0) {
                    // Build route from GPS samples
                    const newPoints = data.gps_samples.map(sample => {
                        if (sample.gps && sample.gps.latitude && sample.gps.longitude) {
                            return [sample.gps.latitude, sample.gps.longitude];
                        } else if (sample.latitude && sample.longitude) {
                            return [sample.latitude, sample.longitude];
                        }
                        return null;
                    }).filter(p => p !== null);

                    if (newPoints.length > 0) {
                        // Update polyline with new route
                        routePolyline.setLatLngs(newPoints);

                        // Zoom to show entire route on first update
                        if (allRoutePoints.length === 0 && newPoints.length > 1) {
                            map.fitBounds(routePolyline.getBounds(), {padding: [50, 50]});
                        }

                        allRoutePoints = newPoints;
                    }
                }
            } catch (error) {
                console.error('Error fetching route data:', error);
            }
        }

        // Show inactive state
        function showInactive() {
            document.getElementById('statusBadge').className = 'status-badge status-inactive';
            document.getElementById('statusBadge').textContent = 'INACTIVE';
            document.getElementById('sessionInfo').textContent = 'No active tracking session';
        }

        // Show stale state
        function showStale(fileAge) {
            document.getElementById('statusBadge').className = 'status-badge status-stale';
            document.getElementById('statusBadge').textContent = 'STALE';
            document.getElementById('sessionInfo').textContent = `Session inactive for ${Math.floor(fileAge)}s`;
        }

        // Show error
        function showError(message) {
            document.getElementById('statusBadge').className = 'status-badge status-inactive';
            document.getElementById('statusBadge').textContent = 'ERROR';
            document.getElementById('sessionInfo').textContent = message;
        }

        // Initialize on page load
        window.addEventListener('DOMContentLoaded', () => {
            initMap();
            pollLiveStatus();

            // Poll every 1 second for updates
            pollInterval = setInterval(pollLiveStatus, 1000);
        });

        // Cleanup on page unload
        window.addEventListener('beforeunload', () => {
            if (pollInterval) {
                clearInterval(pollInterval);
            }
        });
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@app.get("/")
def root():
    """Serve the main dashboard HTML"""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Motion Tracker Dashboard</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: #f5f5f5;
            color: #333;
        }

        .container {
            display: flex;
            height: 100vh;
        }

        .sidebar {
            width: 350px;
            background: white;
            border-right: 1px solid #ddd;
            overflow-y: auto;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }

        .header {
            padding: 20px;
            border-bottom: 1px solid #ddd;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }

        .header h1 {
            font-size: 20px;
            font-weight: 600;
            margin-bottom: 5px;
        }

        .header p {
            font-size: 13px;
            opacity: 0.9;
        }

        .live-monitor-link {
            display: inline-block;
            margin-top: 10px;
            padding: 8px 15px;
            background: rgba(255, 255, 255, 0.2);
            color: white;
            text-decoration: none;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            transition: background 0.2s;
        }

        .live-monitor-link:hover {
            background: rgba(255, 255, 255, 0.3);
        }

        .drives-list {
            list-style: none;
        }

        .drive-item {
            padding: 15px 20px;
            border-bottom: 1px solid #eee;
            cursor: pointer;
            transition: background 0.2s;
        }

        .drive-item:hover {
            background: #f9f9f9;
        }

        .drive-item.active {
            background: #e8f0f8;
            border-left: 4px solid #667eea;
            padding-left: 16px;
        }

        .drive-date {
            font-weight: 600;
            font-size: 14px;
            color: #333;
            margin-bottom: 5px;
        }

        .drive-stats {
            font-size: 12px;
            color: #666;
            line-height: 1.6;
        }

        .stat-badge {
            display: inline-block;
            background: #f0f0f0;
            padding: 2px 8px;
            border-radius: 3px;
            margin: 2px 4px 2px 0;
            font-size: 11px;
            white-space: nowrap;
        }

        .map-container {
            flex: 1;
            position: relative;
        }

        #map {
            width: 100%;
            height: 100%;
        }

        .map-info {
            position: absolute;
            bottom: 20px;
            right: 20px;
            background: white;
            padding: 15px 20px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
            font-size: 13px;
            max-width: 250px;
        }

        .loading {
            text-align: center;
            padding: 40px 20px;
            color: #999;
        }

        .loading spinner {
            display: block;
            width: 40px;
            height: 40px;
            margin: 0 auto 10px;
            border: 3px solid #f0f0f0;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .error {
            color: #d32f2f;
            padding: 20px;
            background: #ffebee;
            border-left: 4px solid #d32f2f;
            margin: 20px;
            border-radius: 4px;
            font-size: 13px;
        }

        @media (max-width: 768px) {
            .container {
                flex-direction: column;
            }

            .sidebar {
                width: 100%;
                height: 40vh;
                border-right: none;
                border-bottom: 1px solid #ddd;
            }

            .map-container {
                height: 60vh;
            }

            .map-info {
                bottom: 10px;
                right: 10px;
                max-width: 90%;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="sidebar">
            <div class="header">
                <h1>Motion Tracker</h1>
                <p>Your Drives</p>
                <a href="/live" class="live-monitor-link">View Live Monitor</a>
            </div>
            <ul class="drives-list" id="drivesList">
                <div class="loading">Loading drives...</div>
            </ul>
        </div>
        <div class="map-container">
            <div id="map"></div>
            <div class="map-info" id="mapInfo" style="display:none;">
                <div id="driveInfoContent"></div>
            </div>
            <div id="statusIndicator" style="display:none; position:absolute; top:10px; left:50%; transform:translateX(-50%); background:#fff; padding:10px 20px; border-radius:4px; box-shadow:0 2px 4px rgba(0,0,0,0.2); z-index:1000; font-size:13px;"></div>
        </div>
    </div>

    <script>
        let map = null;
        let currentGpxLayer = null;
        let drives = [];

        // Show status message on page (visible on mobile)
        function showStatus(message, type = 'info', duration = 3000) {
            const indicator = document.getElementById('statusIndicator');
            indicator.textContent = message;
            indicator.style.display = 'block';
            indicator.style.background = type === 'error' ? '#ffebee' :
                                        type === 'success' ? '#e8f5e9' : '#fff';
            indicator.style.color = type === 'error' ? '#d32f2f' :
                                   type === 'success' ? '#2e7d32' : '#333';
            indicator.style.borderLeft = `4px solid ${type === 'error' ? '#d32f2f' :
                                                      type === 'success' ? '#2e7d32' : '#1976d2'}`;

            if (duration > 0) {
                setTimeout(() => {
                    indicator.style.display = 'none';
                }, duration);
            }
        }

        // Initialize map
        function initMap() {
            map = L.map('map');  // No default view, will auto-fit to drives
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '© OpenStreetMap contributors',
                maxZoom: 19,
            }).addTo(map);
            // Set fallback view if no drives are loaded
            map.setView([40, -95], 4);
        }

        // Fetch and display drives list
        async function loadDrives() {
            try {
                const response = await fetch('/api/drives');
                const data = await response.json();
                drives = data.drives;

                if (drives.length === 0) {
                    document.getElementById('drivesList').innerHTML =
                        '<div class="error">No drives found. Start a tracking session!</div>';
                    return;
                }

                renderDrivesList();

                // Auto-select first drive if it has GPX
                const firstWithGpx = drives.find(d => d.has_gpx);
                if (firstWithGpx) {
                    selectDrive(firstWithGpx.id);
                }
            } catch (error) {
                console.error('Error loading drives:', error);
                document.getElementById('drivesList').innerHTML =
                    `<div class="error">Error loading drives: ${error.message}</div>`;
            }
        }

        // Render drives list
        function renderDrivesList() {
            const list = document.getElementById('drivesList');
            list.innerHTML = drives.map(drive => `
                <li class="drive-item" data-drive-id="${drive.id}" onclick="selectDrive('${drive.id}')">
                    <div class="drive-date">${drive.datetime}</div>
                    <div class="drive-stats">
                        ${drive.stats.distance_km ? `<span class="stat-badge">${drive.stats.distance_km} km</span>` : ''}
                        <span class="stat-badge">${drive.stats.gps_samples} GPS</span>
                        <span class="stat-badge">${Math.round(drive.file_size_mb)} MB</span>
                        ${!drive.has_gpx ? '<span class="stat-badge" style="background:#ffeaa7">No GPX</span>' : ''}
                    </div>
                </li>
            `).join('');
        }

        // Select and display a drive
        async function selectDrive(driveId) {
            // Update UI
            document.querySelectorAll('.drive-item').forEach(item => item.classList.remove('active'));
            const selected = document.querySelector(`.drive-item[data-drive-id="${driveId}"]`);
            if (selected) selected.classList.add('active');

            // Get drive details
            try {
                showStatus('Loading drive details...', 'info', 2000);
                const response = await fetch(`/api/drive/${driveId}`);
                const drive = await response.json();

                // Update map info
                const mapInfo = document.getElementById('mapInfo');
                const infoContent = document.getElementById('driveInfoContent');
                infoContent.innerHTML = `
                    <strong>${drive.datetime}</strong><br>
                    Distance: ${drive.stats.distance_km} km<br>
                    GPS Points: ${drive.stats.gps_samples}<br>
                    Accel Samples: ${drive.stats.accel_samples}<br>
                    Peak Memory: ${drive.stats.peak_memory_mb} MB
                `;
                mapInfo.style.display = 'block';

                // Load GPX if available
                if (drive.has_gpx) {
                    console.log('Drive has GPX - loading map...');
                    loadGpxOnMap(driveId);
                } else {
                    console.log('Drive has no GPX data');
                    showStatus('No GPS data available for this drive', 'error', 5000);
                    if (currentGpxLayer) {
                        map.removeLayer(currentGpxLayer);
                        currentGpxLayer = null;
                    }
                }
            } catch (error) {
                console.error('Error loading drive details:', error);
                showStatus(`Error loading drive: ${error.message}`, 'error', 10000);
            }
        }

        // Load and display GPX on map
        async function loadGpxOnMap(driveId) {
            try {
                showStatus('Loading route...', 'info', 0);

                const response = await fetch(`/api/drive/${driveId}/gpx`);
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }
                const gpxText = await response.text();
                console.log(`GPX loaded: ${gpxText.length} chars`);
                showStatus(`GPX loaded: ${gpxText.length} chars`, 'info', 2000);

                // Remove previous layer
                if (currentGpxLayer) {
                    map.removeLayer(currentGpxLayer);
                }

                // Parse and display GPX
                showStatus('Parsing GPS data...', 'info', 0);
                const geoJSON = gpxToGeoJSON(gpxText);
                console.log('GeoJSON features:', geoJSON.features.length);
                console.log('Coordinates count:', geoJSON.features[1]?.geometry?.coordinates?.length || 0);

                const coordCount = geoJSON.features[1]?.geometry?.coordinates?.length || 0;
                showStatus(`Parsed ${coordCount} GPS points`, 'info', 2000);

                const gpxLayer = L.geoJSON(geoJSON, {
                    style: {
                        color: '#667eea',
                        weight: 3,
                        opacity: 0.8,
                    },
                    pointToLayer: (feature, latlng) => {
                        if (feature.properties.type === 'start') {
                            return L.circleMarker(latlng, {
                                radius: 6,
                                fillColor: '#4caf50',
                                color: '#fff',
                                weight: 2,
                                opacity: 1,
                                fillOpacity: 0.8,
                            });
                        } else if (feature.properties.type === 'end') {
                            return L.circleMarker(latlng, {
                                radius: 6,
                                fillColor: '#f44336',
                                color: '#fff',
                                weight: 2,
                                opacity: 1,
                                fillOpacity: 0.8,
                            });
                        }
                        return L.circleMarker(latlng, {radius: 2, color: '#667eea'});
                    },
                });

                showStatus('Rendering map...', 'info', 0);
                gpxLayer.addTo(map);
                currentGpxLayer = gpxLayer;

                // Fit map to bounds
                const bounds = gpxLayer.getBounds();
                console.log('Bounds valid:', bounds.isValid());
                if (bounds.isValid()) {
                    map.fitBounds(bounds, {padding: [50, 50]});
                    showStatus('Route loaded successfully!', 'success', 3000);
                } else {
                    showStatus('Warning: Invalid bounds - route may not display', 'error', 5000);
                    console.error('Invalid bounds - no route to display');
                }
            } catch (error) {
                console.error('Error loading GPX:', error);
                showStatus(`Error loading route: ${error.message}`, 'error', 10000);
            }
        }

        // Simple GPX to GeoJSON converter
        function gpxToGeoJSON(gpxString) {
            const parser = new DOMParser();
            const xmlDoc = parser.parseFromString(gpxString, "text/xml");

            // Check for XML parsing errors
            const parserError = xmlDoc.querySelector('parsererror');
            if (parserError) {
                console.error('XML parse error:', parserError.textContent);
                throw new Error('Failed to parse GPX XML');
            }

            const coordinates = [];

            // Get trackpoints - now without namespace handling needed
            const trackpoints = Array.from(xmlDoc.getElementsByTagName('trkpt'));
            console.log(`Parsing ${trackpoints.length} trackpoints`);

            trackpoints.forEach((pt, idx) => {
                const lat = pt.getAttribute('lat');
                const lon = pt.getAttribute('lon');

                if (lat === null || lon === null) {
                    console.warn(`Sample ${idx}: Missing lat/lon attributes`);
                    return;
                }

                const latNum = parseFloat(lat);
                const lonNum = parseFloat(lon);

                if (isNaN(latNum) || isNaN(lonNum)) {
                    console.warn(`Sample ${idx}: Invalid numeric values - lat=${lat}, lon=${lon}`);
                    return;
                }

                if (latNum < -90 || latNum > 90 || lonNum < -180 || lonNum > 180) {
                    console.warn(`Sample ${idx}: Out of range - lat=${latNum}, lon=${lonNum}`);
                    return;
                }

                coordinates.push([lonNum, latNum]);
            });

            console.log(`Extracted ${coordinates.length} valid coordinates`);
            console.log(`Coordinate sample:`, coordinates.slice(0, 3));

            if (coordinates.length === 0) {
                throw new Error(`No valid coordinates found in GPX (processed ${trackpoints.length} trackpoints)`);
            }

            return {
                type: 'FeatureCollection',
                features: [
                    {
                        type: 'Feature',
                        properties: {type: 'start'},
                        geometry: {type: 'Point', coordinates: coordinates[0]}
                    },
                    {
                        type: 'Feature',
                        properties: {type: 'route'},
                        geometry: {type: 'LineString', coordinates}
                    },
                    {
                        type: 'Feature',
                        properties: {type: 'end'},
                        geometry: {type: 'Point', coordinates: coordinates[coordinates.length - 1]}
                    }
                ]
            };
        }

        // Initialize on page load
        window.addEventListener('DOMContentLoaded', () => {
            initMap();
            loadDrives();
        });
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    import uvicorn
    port = 8000
    print(f"Starting Motion Tracker Dashboard on http://localhost:{port}")
    print(f"Sessions directory: {SESSIONS_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
