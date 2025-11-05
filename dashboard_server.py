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
