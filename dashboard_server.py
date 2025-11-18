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
import pickle
import time
import sys

app = FastAPI(title="Motion Tracker Dashboard")

# Configuration
BASE_DIR = os.path.expanduser("~/gojo")
SESSIONS_DIR = os.path.join(BASE_DIR, "motion_tracker_sessions")
SESSIONS_SUBDIR = os.path.join(BASE_DIR, "sessions")  # Also check here for motion_tracker_v2 runs
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Ensure repo modules (tools, motion_tracker_v2) are importable
# Insert BASE_DIR so FastAPI worker loads the same file we're editing
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from tools import replay_session as replay_mod
except ImportError:
    replay_mod = None


def maybe_replay_trajectories(data: dict) -> dict:
    """Reconstruct EKF/complementary trajectories if the session lacks step-by-step points."""
    trajectories = data.get("trajectories") or {}
    has_dense = any(len(trajectories.get(key, [])) >= 20 for key in ("ekf", "complementary", "es_ekf"))
    if has_dense or replay_mod is None:
        return trajectories

    try:
        events, start_offset = replay_mod.build_events(data)
        if not events:
            return trajectories
        include_es = True
        replayed = replay_mod.replay_session(
            data,
            start_timestamp=-start_offset,
            include_es=include_es,
        )
    except Exception as exc:
        print(f"Replay fallback failed: {exc}")
        return trajectories

    def convert(points):
        converted = []
        for pt in points or []:
            lat = pt.get("lat")
            lon = pt.get("lon")
            if lat is None or lon is None:
                continue
            converted.append({
                "lat": lat,
                "lon": lon,
                "timestamp": pt.get("timestamp"),
                "uncertainty_m": pt.get("uncertainty_m"),
            })
        return converted

    result = dict(trajectories)
    result["ekf"] = convert(replayed.get("ekf"))
    result["complementary"] = convert(replayed.get("complementary"))
    if "es_ekf" in replayed:
        result["es_ekf"] = convert(replayed.get("es_ekf"))
    if "gps" in replayed and not trajectories.get("gps"):
        result["gps"] = convert(replayed.get("gps"))
    return result

# Metadata cache configuration
CACHE_FILE = os.path.join(SESSIONS_DIR, '.drive_cache.pkl')
CACHE_VERSION = 1


def get_cached_metadata():
    """Load cached drive metadata with version check"""
    if not os.path.exists(CACHE_FILE):
        return {}

    try:
        with open(CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)
            if cache.get('version') != CACHE_VERSION:
                return {}
            return cache.get('drives', {})
    except Exception as e:
        print(f"Cache load error: {e}")
        return {}


def save_cached_metadata(drives_meta):
    """Save drive metadata to cache"""
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump({
                'version': CACHE_VERSION,
                'drives': drives_meta,
                'updated': time.time()
            }, f)
    except Exception as e:
        print(f"Cache write error: {e}")


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

        trajectories = maybe_replay_trajectories(data)
        if not gps_points and trajectories.get("gps"):
            gps_points = trajectories["gps"]

        if not gps_points:
            raise ValueError("No GPS data found in JSON file")
        # trajectories already replayed (above)
        ekf_track = trajectories.get("ekf", [])
        comp_track = trajectories.get("complementary", [])
        es_track = trajectories.get("es_ekf", [])

        if not gps_points and not ekf_track and not comp_track and not es_track:
            raise ValueError("No trajectory data available")

        def fmt_time(value):
            if value is None or value == "":
                return ""
            if isinstance(value, str):
                return value if value.endswith("Z") else f"{value}Z"
            # Numeric timestamps in comparison files are relative seconds; omit if not ISO
            try:
                float(value)
            except (TypeError, ValueError):
                return ""
            return ""

        def append_track(lines, name, desc, points):
            if not points:
                return
            lines.append("  <trk>")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <desc>{desc}</desc>")
            lines.append("    <trkseg>")
            for p in points:
                lat = p.get("latitude") or p.get("lat")
                lon = p.get("longitude") or p.get("lon")
                if lat is None or lon is None:
                    continue
                lines.append(f'      <trkpt lat="{lat}" lon="{lon}">')
                if "altitude" in p:
                    lines.append(f'        <ele>{p["altitude"]}</ele>')
                ts = p.get("timestamp") or p.get("time")
                ts_fmt = fmt_time(ts)
                if ts_fmt:
                    lines.append(f"        <time>{ts_fmt}</time>")
                unc = p.get("uncertainty_m")
                if unc is not None:
                    lines.append(f'        <extensions><uncertainty>{unc:.2f}</uncertainty></extensions>')
                lines.append("      </trkpt>")
            lines.append("    </trkseg>")
            lines.append("  </trk>")

        gpx_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="Motion Tracker Dashboard">',
            '  <metadata>',
            f'    <time>{datetime.now().isoformat()}Z</time>',
            '    <desc>GPS + filtered trajectories</desc>',
            '  </metadata>'
        ]
        append_track(gpx_lines, "GPS", "Raw GPS fixes", gps_points)
        append_track(gpx_lines, "EKF", "Extended Kalman Filter trajectory", ekf_track)
        append_track(gpx_lines, "Complementary", "Complementary filter trajectory", comp_track)
        append_track(gpx_lines, "ES-EKF", "Error-state EKF trajectory", es_track)
        gpx_lines.append("</gpx>\n")

        return "\n".join(gpx_lines)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to generate GPX: {str(e)}")


def lazy_has_gps_data(data: dict) -> bool:
    """Fast check for GPS data - early exit after finding first valid coordinate"""
    # Check nested format (sample["gps"]["latitude"])
    if "gps_data" in data and isinstance(data["gps_data"], list):
        for sample in data["gps_data"][:10]:  # Only check first 10 samples
            if isinstance(sample, dict) and "gps" in sample and isinstance(sample["gps"], dict):
                if "latitude" in sample["gps"] and "longitude" in sample["gps"]:
                    return True

    # Check gps_samples array
    if "gps_samples" in data and isinstance(data["gps_samples"], list):
        for sample in data["gps_samples"][:10]:  # Only check first 10 samples
            if isinstance(sample, dict):
                # Nested format
                if "gps" in sample and isinstance(sample["gps"], dict):
                    if "latitude" in sample["gps"] and "longitude" in sample["gps"]:
                        return True
                # Flat format (comparison files)
                elif "latitude" in sample and "longitude" in sample:
                    return True

    return False


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
def list_drives(limit: int = 20, offset: int = 0):
    """List available drives (paginated - load only requested batch)"""
    # Phase 1: Scan filesystem for all file paths (FAST - no JSON parsing)
    all_filepaths = []

    if os.path.exists(SESSIONS_SUBDIR):
        for session_dir in os.listdir(SESSIONS_SUBDIR):
            session_path = os.path.join(SESSIONS_SUBDIR, session_dir)
            if not os.path.isdir(session_path):
                continue
            for filename in os.listdir(session_path):
                if filename.startswith("motion_track_v2_") and filename.endswith(".json"):
                    all_filepaths.append(os.path.join(session_path, filename))

    if os.path.exists(SESSIONS_DIR):
        for filename in os.listdir(SESSIONS_DIR):
            if (filename.startswith("motion_track_v2_") or filename.startswith("comparison_")) and filename.endswith(".json"):
                all_filepaths.append(os.path.join(SESSIONS_DIR, filename))

    # Phase 2: Sort by modification time (filesystem only, no JSON parsing)
    all_filepaths.sort(key=lambda x: os.path.getmtime(x), reverse=True)

    total = len(all_filepaths)

    # Phase 3: PAGINATE BEFORE LOADING - only load requested batch
    paginated_filepaths = all_filepaths[offset:offset + limit]

    # Phase 4: Load and process only paginated files
    cache = get_cached_metadata()
    drives = []

    for filepath in paginated_filepaths:
        filename = os.path.basename(filepath)
        try:
            # Check cache first (mtime validation)
            mtime = os.path.getmtime(filepath)
            if filename in cache and cache[filename].get('mtime') == mtime:
                # Cache hit - use cached metadata
                metadata = cache[filename]
                drives.append(metadata)
                continue

            # Cache miss - load JSON and process
            data = load_json_file(filepath)
            stats = get_drive_stats(data)
            timestamp = parse_timestamp(filename)
            gpx_filepath = filepath.replace(".json", ".gpx")

            # Check GPX using lazy function (only first 10 samples)
            has_gpx = os.path.exists(gpx_filepath) or lazy_has_gps_data(data)

            metadata = {
                "id": filename.replace(".json", ""),
                "path": filepath,
                "gpx_path": gpx_filepath,
                "timestamp": timestamp.isoformat(),
                "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "has_gpx": has_gpx,
                "file_size_mb": round(os.path.getsize(filepath) / (1024 * 1024), 2),
                "stats": stats,
                "mtime": mtime,
            }

            # Update cache
            cache[filename] = metadata
            drives.append(metadata)

        except Exception as e:
            print(f"Error loading {filename}: {e}")
            continue

    # Save updated cache
    save_cached_metadata(cache)

    return {
        "drives": drives,
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": (offset + limit) < total
    }


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

        tracks_payload = {}
        trajectories = data.get("trajectories")
        if isinstance(trajectories, dict):
            for key, points in trajectories.items():
                if not isinstance(points, list) or not points:
                    continue
                trimmed = points[-400:]
                formatted = []
                for pt in trimmed:
                    lat = pt.get("lat") or pt.get("latitude")
                    lon = pt.get("lon") or pt.get("longitude")
                    if lat is None or lon is None:
                        continue
                    formatted.append({
                        "lat": lat,
                        "lon": lon,
                        "velocity": pt.get("velocity"),
                        "uncertainty": pt.get("uncertainty_m") or pt.get("uncertainty")
                    })
                if formatted:
                    tracks_payload[key] = formatted

        return {
            "gps_samples": gps_samples,
            "total_gps": len(data.get("gps_samples", [])),
            "total_accel": len(data.get("accel_samples", [])),
            "total_gyro": len(data.get("gyro_samples", [])),
            "auto_save": data.get("auto_save", False),
            "autosave_number": data.get("autosave_number", 0),
            "peak_memory_mb": data.get("peak_memory_mb", 0),
            "tracks": tracks_payload
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to load session data: {str(e)}")


def get_theme_css():
    """Shared dark mode CSS"""
    return """
    <script>
        // Initialize theme from localStorage with error handling
        function initTheme() {
            try {
                const isDark = localStorage.getItem('dashboardTheme') === 'dark';
                if (isDark) document.documentElement.setAttribute('data-theme', 'dark');
                updateThemeIcon(isDark);
                return isDark;
            } catch (e) {
                console.warn('localStorage unavailable, theme won\\'t persist');
                return false;
            }
        }
        function updateThemeIcon(isDark) {
            const toggles = document.querySelectorAll('.theme-toggle');
            toggles.forEach(toggle => {
                toggle.textContent = isDark ? '☀️' : '🌙';
                toggle.setAttribute('aria-pressed', isDark ? 'true' : 'false');
            });
        }
        function toggleTheme() {
            try {
                const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
                if (isDark) {
                    document.documentElement.removeAttribute('data-theme');
                    localStorage.setItem('dashboardTheme', 'light');
                    updateThemeIcon(false);
                } else {
                    document.documentElement.setAttribute('data-theme', 'dark');
                    localStorage.setItem('dashboardTheme', 'dark');
                    updateThemeIcon(true);
                }
            } catch (e) {
                console.warn('localStorage write failed, theme won\\'t persist');
            }
        }
    </script>
    <style>
        :root {
            --bg-primary: #f5f5f5;
            --bg-secondary: #fff;
            --bg-tertiary: #f9f9f9;
            --text-primary: #333;
            --text-secondary: #666;
            --text-light: #999;
            --border-color: #ddd;
            --header-bg: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --accent-color: #667eea;
            --map-overlay-bg: rgba(255, 255, 255, 0.95);
        }

        html[data-theme="dark"] {
            --bg-primary: #1a1a1a;
            --bg-secondary: #2a2a2a;
            --bg-tertiary: #333;
            --text-primary: #fff;
            --text-secondary: #bbb;
            --text-light: #999;
            --border-color: #444;
            --header-bg: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --accent-color: #667eea;
            --map-overlay-bg: rgba(42, 42, 42, 0.95);
        }
    </style>
    """


@app.get("/live")
def live_monitor():
    """Serve the live drive monitor page"""
    theme_css = get_theme_css()
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Drive Monitor</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    """ + theme_css + """
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            height: 100vh;
            overflow: hidden;
        }

        .container {
            display: flex;
            height: 100vh;
        }

        .sidebar {
            width: 320px;
            background: var(--bg-secondary);
            border-right: 1px solid var(--border-color);
            overflow-y: auto;
            display: flex;
            flex-direction: column;
        }

        .header {
            padding: 20px;
            background: var(--header-bg);
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
            background: var(--bg-tertiary);
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 15px;
        }

        .metric-group h3 {
            font-size: 12px;
            color: var(--text-light);
            text-transform: uppercase;
            margin-bottom: 10px;
            letter-spacing: 0.5px;
        }

        .metric-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid var(--border-color);
        }

        .metric-row:last-child {
            border-bottom: none;
        }

        .metric-label {
            font-size: 13px;
            color: var(--text-secondary);
        }

        .metric-value {
            font-size: 14px;
            font-weight: 600;
            color: var(--text-primary);
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
            background: var(--bg-tertiary);
            padding: 8px;
            margin: 5px 0;
            border-radius: 4px;
            font-size: 12px;
        }

        .incident-swerving { border-left: 3px solid #ff9800; }
        .incident-braking { border-left: 3px solid #f44336; }
        .incident-impact { border-left: 3px solid #9c27b0; }

        .theme-toggle {
            position: absolute;
            top: 20px;
            right: 20px;
            background: rgba(255, 255, 255, 0.2);
            border: none;
            color: white;
            padding: 8px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 18px;
            transition: background 0.2s;
        }

        .theme-toggle:hover {
            background: rgba(255, 255, 255, 0.3);
        }

        .speedometer {
            width: 100%;
            height: 160px;
            margin: 10px 0;
            position: relative;
            display: flex;
            align-items: flex-end;
        }

        .speedometer-gauge {
            width: 100%;
            height: 100%;
            position: relative;
            background: radial-gradient(ellipse 100% 100% at 50% 100%,
                rgba(76, 175, 80, 0.1) 0%,
                transparent 70%);
            border-bottom: 3px solid var(--border-color);
        }

        .speedometer-track {
            position: absolute;
            width: 100%;
            height: 100%;
            bottom: 0;
            left: 0;
        }

        /* Speed zone backgrounds */
        .speedometer-track::before {
            content: '';
            position: absolute;
            width: 100%;
            height: 8px;
            bottom: 0;
            left: 0;
            background: linear-gradient(
                to right,
                #4caf50 0%,
                #4caf50 25%,
                #ffc107 25%,
                #ffc107 60%,
                #f44336 60%,
                #f44336 100%
            );
            opacity: 0.6;
            border-radius: 2px;
        }

        .speedometer-markers {
            position: absolute;
            width: 100%;
            height: 100%;
            bottom: 12px;
            font-size: 11px;
            color: var(--text-secondary);
            font-weight: 600;
        }

        .speedometer-marker {
            position: absolute;
            bottom: 0;
            transform: translateX(-50%);
        }

        /* Position markers based on actual speed (max 216 km/h) */
        .speedometer-marker:nth-child(1) { left: 0%; }           /* 0 km/h */
        .speedometer-marker:nth-child(2) { left: 18.5%; }        /* 40 km/h */
        .speedometer-marker:nth-child(3) { left: 37%; }          /* 80 km/h */
        .speedometer-marker:nth-child(4) { left: 55.5%; }        /* 120 km/h */
        .speedometer-marker:nth-child(5) { left: 74%; }          /* 160 km/h */
        .speedometer-marker:nth-child(6) { left: 92.6%; }        /* 200+ km/h */

        .speedometer-marker::before {
            content: '';
            position: absolute;
            width: 2px;
            height: 12px;
            background: var(--border-color);
            bottom: -12px;
            left: 50%;
            transform: translateX(-50%);
        }

        .speedometer-needle {
            position: absolute;
            width: 8px;
            height: 100%;
            background: linear-gradient(to right, #ff0000, #ff6b6b);
            left: 0;
            bottom: 0;
            transition: left 0.25s cubic-bezier(0.25, 0.46, 0.45, 0.94);
            border-radius: 2px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
            z-index: 5;
        }

        html[data-theme="dark"] .speedometer-needle {
            background: linear-gradient(to top, #ffffff, #f0f0f0);
        }

        .speedometer-center {
            position: absolute;
            width: 10px;
            height: 10px;
            background: var(--text-primary);
            border-radius: 50%;
            left: 50%;
            bottom: -4px;
            transform: translateX(-50%);
            z-index: 6;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
        }

        .speedometer-value {
            position: absolute;
            bottom: 55%;
            left: 50%;
            transform: translateX(-50%);
            z-index: 10;
            text-align: center;
        }

        .speedometer-value .speed-number {
            font-weight: 700;
            font-size: 32px;
            color: var(--text-primary);
            line-height: 1;
        }

        .speedometer-value .speed-unit {
            font-size: 11px;
            color: var(--text-secondary);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .map-container {
            flex: 1;
            position: relative;
            z-index: 0;
        }

        #map {
            width: 100%;
            height: 100%;
        }

        .map-overlay {
            position: absolute;
            top: 80px;
            right: 20px;
            background: var(--map-overlay-bg);
            padding: 15px 20px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
            z-index: 1000;
            min-width: 200px;
            color: var(--text-primary);
        }

        .map-overlay h3 {
            font-size: 13px;
            color: var(--accent-color);
            margin-bottom: 10px;
            text-transform: uppercase;
        }

        .map-overlay .metric {
            display: flex;
            justify-content: space-between;
            margin: 5px 0;
            font-size: 13px;
            color: var(--text-primary);
        }

        .track-legend {
            margin-top: 15px;
            border-top: 1px solid var(--border-color);
            padding-top: 12px;
        }

        .track-legend h4 {
            font-size: 12px;
            margin-bottom: 8px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .track-toggles {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .track-toggle {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            color: var(--text-primary);
        }

        .track-toggle input {
            accent-color: var(--accent-color);
        }

        .track-color {
            width: 14px;
            height: 14px;
            border-radius: 50%;
            display: inline-block;
            border: 1px solid rgba(0,0,0,0.2);
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
        <button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark mode" aria-label="Toggle dark mode" aria-pressed="false">🌙</button>
        <div class="sidebar">
            <div class="header">
                <h1>Live Drive Monitor</h1>
                <p id="sessionInfo">Waiting for session...</p>
                <div id="statusBadge" class="status-badge status-inactive">INACTIVE</div>
            </div>

            <div class="metrics-panel">
                <div class="metric-group">
                    <h3>Current State</h3>
                    <div class="speedometer">
                        <div class="speedometer-gauge">
                            <div class="speedometer-track">
                                <div class="speedometer-markers">
                                    <div class="speedometer-marker">0</div>
                                    <div class="speedometer-marker">40</div>
                                    <div class="speedometer-marker">80</div>
                                    <div class="speedometer-marker">120</div>
                                    <div class="speedometer-marker">160</div>
                                    <div class="speedometer-marker">200+</div>
                                </div>
                            </div>
                            <div class="speedometer-needle" id="speedNeedle"></div>
                            <div class="speedometer-center"></div>
                        </div>
                        <div class="speedometer-value" id="speedValue">
                            <div class="speed-number">0</div>
                            <div class="speed-unit">km/h</div>
                        </div>
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
            <div class="track-legend">
                <h4>Tracks</h4>
                <div class="track-toggles" id="trackLegend"></div>
            </div>
        </div>
        </div>
    </div>

    <script>
        const PRIMARY_TRACK_KEY = 'es_ekf_dead_reckoning';
        const TRACK_CONFIG = {
            'ekf': { label: 'EKF (GPS)', color: '#22c55e', weight: 3 },
            'es_ekf': { label: 'ES-EKF (GPS)', color: '#a855f7', weight: 3, dashArray: '6 4' },
            'es_ekf_dead_reckoning': { label: 'ES-EKF Dead Reckoning', color: '#ff9800', weight: 4 },
            'complementary': { label: 'Complementary', color: '#f97316', weight: 3, dashArray: '2 6' },
        };
        const DEFAULT_TRACK_VISIBILITY = {
            'es_ekf_dead_reckoning': true,
            'ekf': false,
            'es_ekf': false,
            'complementary': false,
        };

        let map = null;
        let currentMarker = null;
        let trackLayers = {};
        let trackVisibility = { ...DEFAULT_TRACK_VISIBILITY };
        let allRoutePoints = [];
        let currentSessionId = null;
        let pollInterval = null;
        let lastUpdateTime = 0;
        let mapFitToTrack = false;

        // Initialize map
        function initMap() {
            map = L.map('map').setView([37.7749, -122.4194], 13);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '© OpenStreetMap contributors',
                maxZoom: 19,
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
                        mapFitToTrack = false;
                        Object.values(trackLayers).forEach(layer => {
                            if (map && layer && map.hasLayer(layer)) {
                                map.removeLayer(layer);
                            }
                        });
                        trackLayers = {};
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
            const velocityMs = status.current_velocity;
            const velocityKmh = velocityMs * 3.6;
            document.querySelector('#speedValue .speed-number').textContent = velocityKmh.toFixed(0);
            document.getElementById('distance').textContent = `${(status.total_distance / 1000).toFixed(2)} km`;

            // Update speedometer needle (horizontal slider, 0-100% width)
            const maxVelocity = 60; // m/s (216 km/h)
            const percentage = Math.min(100, (velocityMs / maxVelocity) * 100);
            document.getElementById('speedNeedle').style.left = `${percentage}%`;
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

        function ensureTrackLayer(trackKey) {
            if (trackLayers[trackKey]) return trackLayers[trackKey];
            const cfg = TRACK_CONFIG[trackKey] || {};
            const layer = L.polyline([], {
                color: cfg.color || '#667eea',
                weight: cfg.weight || 3,
                opacity: cfg.opacity || 0.9,
                dashArray: cfg.dashArray || null,
            });
            trackLayers[trackKey] = layer;
            if (trackVisibility[trackKey]) {
                layer.addTo(map);
            }
            return layer;
        }

        function updateTrackVisibility(trackKey) {
            const layer = trackLayers[trackKey];
            if (!layer) return;
            if (trackVisibility[trackKey]) {
                layer.addTo(map);
            } else if (map.hasLayer(layer)) {
                map.removeLayer(layer);
            }
        }

        function initTrackLegend() {
            const legend = document.getElementById('trackLegend');
            if (!legend) return;
            legend.innerHTML = Object.entries(TRACK_CONFIG).map(([key, cfg]) => `
                <label class="track-toggle">
                    <input type="checkbox" data-track="${key}" ${trackVisibility[key] ? 'checked' : ''}>
                    <span class="track-color" style="background:${cfg.color || '#667eea'}; ${cfg.dashArray ? 'border-style:dashed;' : ''}"></span>
                    <span>${cfg.label}</span>
                </label>
            `).join('');
            legend.querySelectorAll('input').forEach(input => {
                input.addEventListener('change', (event) => {
                    const track = event.target.dataset.track;
                    trackVisibility[track] = event.target.checked;
                    updateTrackVisibility(track);
                });
            });
        }

        // Fetch route data from auto-save
        async function fetchRouteData(sessionId) {
            try {
                const response = await fetch(`/api/live/data/${sessionId}`);
                const data = await response.json();

                let tracksUpdated = false;
                if (data.tracks && Object.keys(data.tracks).length > 0) {
                    tracksUpdated = true;
                    Object.entries(data.tracks).forEach(([trackKey, points]) => {
                        if (!TRACK_CONFIG[trackKey]) return;
                        const latlngs = points.map(pt => {
                            if (typeof pt.lat === 'number' && typeof pt.lon === 'number') {
                                return [pt.lat, pt.lon];
                            }
                            return null;
                        }).filter(Boolean);

                        const layer = ensureTrackLayer(trackKey);
                        layer.setLatLngs(latlngs);
                        if (trackVisibility[trackKey]) {
                            layer.addTo(map);
                        } else if (map.hasLayer(layer)) {
                            map.removeLayer(layer);
                        }

                        if (trackKey === PRIMARY_TRACK_KEY && latlngs.length > 1) {
                            allRoutePoints = latlngs;
                            if (!mapFitToTrack) {
                                map.fitBounds(layer.getBounds(), { padding: [50, 50] });
                                mapFitToTrack = true;
                            }
                        }
                    });
                }

                // Fallback to legacy GPS samples if no track data yet
                if (!tracksUpdated && data.gps_samples && data.gps_samples.length > 0) {
                    const newPoints = data.gps_samples.map(sample => {
                        if (sample.gps && sample.gps.latitude && sample.gps.longitude) {
                            return [sample.gps.latitude, sample.gps.longitude];
                        } else if (sample.latitude && sample.longitude) {
                            return [sample.latitude, sample.longitude];
                        }
                        return null;
                    }).filter(p => p !== null);

                    if (newPoints.length > 0) {
                        const fallbackLayer = ensureTrackLayer('ekf');
                        fallbackLayer.setLatLngs(newPoints);
                        if (trackVisibility['ekf']) {
                            fallbackLayer.addTo(map);
                        }
                        if (!mapFitToTrack && newPoints.length > 1) {
                            map.fitBounds(fallbackLayer.getBounds(), { padding: [50, 50] });
                            mapFitToTrack = true;
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
        function handleViewportChange() {
            if (!isMobile) return;
            const currentHeight = getViewportHeight();
            baselineMobileHeight = baselineMobileHeight === null
                ? currentHeight
                : Math.min(baselineMobileHeight, currentHeight);
            applySheetHeightFromState();
        }

        window.addEventListener('resize', handleViewportChange);

        if (window.visualViewport && !visualViewportHandlerAttached) {
            window.visualViewport.addEventListener('resize', handleViewportChange);
            visualViewportHandlerAttached = true;
        }

        window.addEventListener('DOMContentLoaded', () => {
            initMap();
            initTrackLegend();
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
    theme_css = get_theme_css()
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Motion Tracker Dashboard</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    """ + theme_css + """
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        :root {
            --sheet-height: 0px;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
        }

        .container {
            display: flex;
            height: 100vh;
            flex-direction: row;
        }

        .sidebar {
            width: 350px;
            background: var(--bg-secondary);
            border-right: 1px solid var(--border-color);
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: width 0.3s ease, transform 0.3s ease;
            position: relative;
            flex: 0 0 350px;
            display: flex;
            flex-direction: column;
        }

        .sheet-handle {
            display: none;
            height: 4px;
            background: var(--border-color);
            border-radius: 2px;
            width: 40px;
            margin: 8px auto 0;
            cursor: grab;
            touch-action: none;
        }

        .header {
            padding: 20px;
            border-bottom: 1px solid var(--border-color);
            background: var(--header-bg);
            color: white;
            position: relative;
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

        .search-bar {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
            padding: 10px 20px;
            border-bottom: 1px solid var(--border-color);
        }

        .search-bar input {
            flex: 1 1 200px;
            min-width: 0;
            padding: 8px 12px;
            min-height: 44px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            font-size: 13px;
        }

        .search-bar input::placeholder {
            color: var(--text-light);
        }

        .filter-toggle {
            padding: 8px 12px;
            min-height: 44px;
            border: 1px solid var(--accent-color);
            border-radius: 6px;
            background: var(--accent-color);
            color: white;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            white-space: nowrap;
            transition: opacity 0.2s;
        }

        .search-bar .filter-toggle {
            flex: 0 0 auto;
        }

        .filter-toggle:active {
            opacity: 0.8;
        }

        .filter-toggle[aria-pressed="false"] {
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border-color: var(--border-color);
        }

        .group-header {
            padding: 10px 20px;
            font-weight: 600;
            font-size: 11px;
            color: var(--text-light);
            text-transform: uppercase;
            background: var(--bg-tertiary);
            border-bottom: 1px solid var(--border-color);
            margin-top: 10px;
        }

        .drives-list {
            list-style: none;
            flex: 1;
            overflow-y: auto;
        }

        .drive-item {
            padding: 15px 20px;
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            transition: background 0.2s;
        }

        .drive-item:hover {
            background: var(--bg-tertiary);
        }

        .drive-item.active {
            background: var(--bg-tertiary);
            border-left: 4px solid var(--accent-color);
            padding-left: 16px;
        }

        .drive-date {
            font-weight: 600;
            font-size: 14px;
            color: var(--text-primary);
            margin-bottom: 5px;
        }

        .drive-stats {
            font-size: 12px;
            color: var(--text-secondary);
            line-height: 1.6;
        }

        .stat-badge {
            display: inline-block;
            background: var(--bg-tertiary);
            color: var(--text-secondary);
            padding: 2px 8px;
            border-radius: 3px;
            margin: 2px 4px 2px 0;
            font-size: 11px;
            white-space: nowrap;
        }

        body.compact-mode .drive-item {
            padding: 10px 16px;
        }

        body.compact-mode .drive-date {
            font-size: 13px;
        }

        body.compact-mode .drive-stats {
            font-size: 11px;
        }

        body.compact-mode .stat-badge {
            font-size: 10px;
            padding: 1px 6px;
        }

        body.compact-mode .search-bar {
            padding: 8px 15px;
        }

        body.compact-mode .filter-toggle {
            min-height: 38px;
            font-size: 11px;
            padding: 6px 10px;
        }

        body.compact-mode .map-info,
        body.compact-mode .track-legend {
            font-size: 12px;
        }

        .drawer-toggle {
            position: absolute;
            top: 14px;
            right: -16px;
            width: 34px;
            height: 34px;
            border-radius: 17px;
            border: 1px solid var(--border-color);
            background: var(--bg-secondary);
            color: var(--text-primary);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 2px 6px rgba(0,0,0,0.15);
            transition: transform 0.3s ease, background 0.2s;
            z-index: 1001;
            font-size: 16px;
        }

        .drawer-toggle:hover {
            background: var(--bg-tertiary);
        }

        @media (min-width: 769px) {
            body.drawer-collapsed .sidebar {
                width: 24px;
                flex: 0 0 24px;
            }

            body.drawer-collapsed .sheet-handle,
            body.drawer-collapsed .header,
            body.drawer-collapsed .search-bar,
            body.drawer-collapsed .drives-list {
                opacity: 0;
                pointer-events: none;
            }

            body.drawer-collapsed .drawer-toggle {
                transform: rotate(180deg);
                right: -18px;
            }
        }

        .theme-toggle {
            position: absolute;
            top: 15px;
            right: 15px;
            background: rgba(255, 255, 255, 0.2);
            border: none;
            color: white;
            padding: 6px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 16px;
            transition: background 0.2s;
            z-index: 100;
        }

        .theme-toggle:hover {
            background: rgba(255, 255, 255, 0.3);
        }

        .map-container {
            flex: 1;
            position: relative;
            z-index: 0;
        }

        #map {
            width: 100%;
            height: 100%;
        }

        .map-info {
            position: absolute;
            bottom: 20px;
            right: 20px;
            background: var(--map-overlay-bg);
            color: var(--text-primary);
            padding: 15px 20px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
            font-size: 13px;
            max-width: 250px;
            z-index: 1000;
            transition: all 0.25s ease;
        }

        .map-info.collapsed {
            padding: 10px 12px;
            border-radius: 20px;
            max-width: none;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .map-info.collapsed #driveInfoContent,
        .map-info.collapsed #trackLegend {
            display: none;
        }

        .legend-bubble-toggle {
            border: none;
            background: var(--accent-color);
            color: white;
            border-radius: 999px;
            padding: 6px 10px;
            cursor: pointer;
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 4px;
            font-weight: 600;
        }

        .legend-bubble-toggle:focus-visible {
            outline: 2px solid var(--accent-color);
            outline-offset: 2px;
        }

        .legend-bubble-toggle span {
            font-size: 16px;
        }

        .track-legend {
            margin-top: 12px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .track-toggle {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
        }

        .track-toggle input {
            accent-color: var(--accent-color);
        }

        .track-color {
            width: 14px;
            height: 14px;
            border-radius: 50%;
            display: inline-block;
            border: 1px solid rgba(0,0,0,0.2);
        }

        .loading {
            text-align: center;
            padding: 40px 20px;
            color: var(--text-light);
        }

        .loading-spinner {
            display: block;
            width: 40px;
            height: 40px;
            margin: 0 auto 10px;
            border: 3px solid var(--border-color);
            border-top: 3px solid var(--accent-color);
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
            background: var(--bg-tertiary);
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
                position: fixed;
                bottom: 0;
                left: 0;
                right: 0;
                width: 100%;
                height: var(--sheet-height, 25vh);
                border-right: none;
                border-top: 1px solid var(--border-color);
                border-bottom: none;
                border-radius: 12px 12px 0 0;
                overflow: hidden;
                display: flex;
                flex-direction: column;
                z-index: 1000;
                box-shadow: 0 -4px 12px rgba(0,0,0,0.15);
            }

            .sheet-handle {
                display: block;
                cursor: grab;
                padding: 8px;
                touch-action: none;
            }

            .sheet-handle:active {
                cursor: grabbing;
            }

            .header {
                display: none;
            }

            .search-bar {
                padding: 10px 20px 0;
                border-bottom: none;
                flex-shrink: 0;
            }

            .drives-list {
                flex: 1;
                overflow-y: auto;
            }

            .map-container {
                flex: none;
                position: relative;
                z-index: 0;
                height: calc(100vh - var(--sheet-height, 0px));
            }

            .map-info {
                bottom: calc(var(--sheet-height, 0px) + 20px);
                right: 10px;
                max-width: 90%;
                font-size: 12px;
            }

            .theme-toggle {
                position: absolute;
                top: 15px;
                right: 15px;
                background: rgba(0, 0, 0, 0.5);
                z-index: 1001;
            }

            .theme-toggle:hover {
                background: rgba(0, 0, 0, 0.6);
            }

            .drawer-toggle {
                width: 42px;
                height: 42px;
                border-radius: 21px;
                right: 16px;
                bottom: 16px;
                top: auto;
                transform: none !important;
            }
        }

        @media (max-width: 400px) {
            .theme-toggle {
                padding: 4px 8px;
                font-size: 14px;
            }

            .header {
                padding-bottom: 35px;
            }

            .map-overlay {
                top: 60px;
                right: 10px;
                font-size: 11px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="sidebar sheet-half" id="bottomSheet">
            <div class="sheet-handle" id="sheetHandle" role="button" tabindex="0" aria-label="Adjust drive list panel"></div>
            <button class="drawer-toggle" id="drawerToggle" aria-pressed="false" aria-label="Collapse drive list" title="Collapse drive list">‹</button>
            <div class="header">
                <button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark mode" aria-label="Toggle dark mode" aria-pressed="false">🌙</button>
                <h1>Motion Tracker</h1>
                <p>Your Drives</p>
                <a href="/live" class="live-monitor-link">View Live Monitor</a>
            </div>
            <div class="search-bar">
                <input type="text" id="searchInput" placeholder="Search drives...">
                <button id="gpsFilterBtn" class="filter-toggle" title="Show/hide runs without GPS data" aria-pressed="true">Show GPS only</button>
                <button id="compactModeBtn" class="filter-toggle" title="Toggle compact drive list view" aria-pressed="false">Compact view</button>
            </div>
            <ul class="drives-list" id="drivesList">
                <div class="loading">Loading drives...</div>
            </ul>
        </div>
        <div class="map-container">
            <div id="map"></div>
            <div class="map-info collapsed" id="mapInfo" style="display:none;">
                <button id="legendToggleBtn" class="legend-bubble-toggle" aria-expanded="false" aria-controls="trackLegend">
                    <span aria-hidden="true">☰</span>
                    <span id="legendBubbleStatus">Stats</span>
                </button>
                <div id="driveInfoContent"></div>
                <div id="trackLegend" class="track-legend"></div>
            </div>
            <div id="statusIndicator" style="display:none; position:absolute; top:10px; left:50%; transform:translateX(-50%); background:#fff; padding:10px 20px; border-radius:4px; box-shadow:0 2px 4px rgba(0,0,0,0.2); z-index:1000; font-size:13px;"></div>
        </div>
    </div>

    <script>
        let map = null;
        let trackLayers = {};
        let trackVisibility = {};
        let currentGpxLayers = [];
        const TRACK_COLORS = {
            'GPS': '#2563eb',
            'EKF': '#22c55e',
            'Complementary': '#f97316',
            'ES-EKF': '#a855f7'
        };
        const TRACK_PRIORITY = ['ES-EKF', 'EKF', 'GPS', 'Complementary'];
        const SHEET_HEIGHT_RATIOS = {
            collapsed: 0.15,
            half: 0.55,
            expanded: 0.88,
        };
        const COLLAPSED_EXTRA_MULTIPLIER = 0.9;
        const COLLAPSED_EXTRA_MAX_RATIO = 0.25;  // Boost up to 25% of viewport
        const COLLAPSED_ABSOLUTE_MAX_RATIO = 0.55;  // Never cover more than 55% while "collapsed"
        let drives = [];
        let displayedDrives = [];
        const DRIVES_PER_PAGE = 15;
        let currentPage = 0;
        let isLoadingMore = false;
        let showNonGpsRuns = false;  // Filter state: false = show GPS only (default)
        let isCompactMode = false;
        let isDrawerCollapsed = false;
        let isLegendCollapsed = true;
        let baselineMobileHeight = null;
        let visualViewportHandlerAttached = false;

        const mobileMediaQuery = window.matchMedia('(max-width: 768px)');
        let isMobile = mobileMediaQuery.matches;

        // Bottom sheet management
        let sheetState = isMobile ? 'half' : 'expanded';  // default to half only on mobile
        let sheetDragInitialized = false;

        function setSheetHeightPixels(pixels) {
            const sheet = document.getElementById('bottomSheet');
            const root = document.documentElement;
            if (!sheet || !root) return;
            const clamped = Math.max(0, Math.min(pixels, window.innerHeight));
            sheet.style.setProperty('--sheet-height', `${clamped}px`);
            root.style.setProperty('--sheet-height', `${clamped}px`);
        }

        function getViewportHeight() {
            return window.visualViewport ? window.visualViewport.height : window.innerHeight;
        }

        function applySheetHeightFromState() {
            if (!isMobile) {
                const sheet = document.getElementById('bottomSheet');
                sheet?.style.removeProperty('--sheet-height');
                document.documentElement.style.setProperty('--sheet-height', '0px');
                return;
            }
            const viewportHeight = getViewportHeight();
            const ratio = SHEET_HEIGHT_RATIOS[sheetState] || SHEET_HEIGHT_RATIOS.half;
            let targetHeight = viewportHeight * ratio;
            if (sheetState === 'collapsed') {
                const baseline = baselineMobileHeight ?? viewportHeight;
                const extraPixels = Math.max(0, viewportHeight - baseline);
                const chromePixels = Math.max(0, window.innerHeight - viewportHeight);
                if (extraPixels > 0) {
                    const maxExtra = viewportHeight * COLLAPSED_EXTRA_MAX_RATIO;
                    const boost = Math.min((extraPixels + chromePixels) * COLLAPSED_EXTRA_MULTIPLIER, maxExtra);
                    targetHeight += boost;
                }
                const maxCollapsed = viewportHeight * COLLAPSED_ABSOLUTE_MAX_RATIO;
                targetHeight = Math.min(targetHeight, maxCollapsed);
            }
            setSheetHeightPixels(targetHeight);
        }

        function setSheetState(state) {
            const sheet = document.getElementById('bottomSheet');
            if (!sheet) return;
            sheet.classList.remove('sheet-collapsed', 'sheet-half', 'sheet-expanded');
            sheet.classList.add(`sheet-${state}`);
            sheetState = state;
            applySheetHeightFromState();
            updateDrawerToggleButton();
        }

        function toggleSheetState() {
            if (sheetState === 'collapsed') {
                setSheetState('half');
            } else if (sheetState === 'half') {
                setSheetState('expanded');
            } else {
                setSheetState('collapsed');
            }
        }

        mobileMediaQuery.addEventListener('change', (event) => {
            isMobile = event.matches;
            const sheet = document.getElementById('bottomSheet');
            if (!sheet) return;
            if (isMobile && isDrawerCollapsed) {
                isDrawerCollapsed = false;
                document.body.classList.remove('drawer-collapsed');
                updateDrawerToggleButton();
            } else if (!isMobile) {
                updateDrawerToggleButton();
            }
            if (!isMobile) {
                document.documentElement.style.setProperty('--sheet-height', '0px');
                baselineMobileHeight = null;
            } else {
                baselineMobileHeight = window.innerHeight;
                setSheetState(sheetState === 'collapsed' ? 'collapsed' : 'half');
            }
            applySheetHeightFromState();
        });

        // Bottom sheet drag handling
        function initSheetDrag() {
            const handle = document.getElementById('sheetHandle');
            const sheet = document.getElementById('bottomSheet');
            if (!handle || !sheet || sheetDragInitialized) return;
            sheetDragInitialized = true;

            let isDragging = false;
            let startY = 0;
            let startHeight = 0;
            let pointerMoved = false;
            let activePointerId = null;

            const clampHeight = (height) => {
                const maxHeight = window.innerHeight * SHEET_HEIGHT_RATIOS.expanded;
                const minHeightPx = window.innerHeight * SHEET_HEIGHT_RATIOS.collapsed;
                return Math.min(Math.max(height, minHeightPx), maxHeight);
            };

            const beginDrag = (clientY) => {
                if (!isMobile) return false;
                isDragging = true;
                pointerMoved = false;
                startY = clientY;
                startHeight = sheet.offsetHeight;
                handle.style.cursor = 'grabbing';
                return true;
            };

            const updateDrag = (clientY) => {
                if (!isDragging || !isMobile) return;
                const diff = startY - clientY;
                if (Math.abs(diff) > 3) {
                    pointerMoved = true;
                }
                const newHeight = clampHeight(startHeight + diff);
                setSheetHeightPixels(newHeight);
            };

            const commitDrag = () => {
                if (!isDragging) return;
                handle.style.cursor = 'grab';
                const moved = pointerMoved;
                isDragging = false;
                pointerMoved = false;
                activePointerId = null;

                if (!isMobile) {
                    return;
                }

                if (!moved) {
                    toggleSheetState();
                    return;
                }

                const currentHeight = sheet.offsetHeight;
                const collapsedHeight = window.innerHeight * SHEET_HEIGHT_RATIOS.collapsed;
                const halfHeight = window.innerHeight * SHEET_HEIGHT_RATIOS.half;
                const expandedHeight = window.innerHeight * SHEET_HEIGHT_RATIOS.expanded;
                const threshold1 = (collapsedHeight + halfHeight) / 2;
                const threshold2 = (halfHeight + expandedHeight) / 2;

                if (currentHeight < threshold1) {
                    setSheetState('collapsed');
                } else if (currentHeight < threshold2) {
                    setSheetState('half');
                } else {
                    setSheetState('expanded');
                }
            };

            if (window.PointerEvent) {
                handle.addEventListener('pointerdown', (event) => {
                    if (!beginDrag(event.clientY)) return;
                    activePointerId = event.pointerId;
                    handle.setPointerCapture(event.pointerId);
                });

                handle.addEventListener('pointermove', (event) => {
                    if (!isDragging || event.pointerId !== activePointerId) return;
                    updateDrag(event.clientY);
                });

                const finishPointer = (event) => {
                    if (event.pointerId !== activePointerId) return;
                    handle.releasePointerCapture?.(event.pointerId);
                    commitDrag();
                };

                handle.addEventListener('pointerup', finishPointer);
                handle.addEventListener('pointercancel', finishPointer);
                handle.addEventListener('pointerleave', finishPointer);
            } else {
                handle.addEventListener('touchstart', (event) => {
                    const touch = event.touches[0];
                    if (!touch) return;
                    if (beginDrag(touch.clientY)) {
                        event.preventDefault();
                    }
                }, {passive: false});

                window.addEventListener('touchmove', (event) => {
                    if (!isDragging) return;
                    const touch = event.touches[0];
                    if (!touch) return;
                    updateDrag(touch.clientY);
                    event.preventDefault();
                }, {passive: false});

                const finishTouch = () => {
                    if (!isDragging) return;
                    commitDrag();
                };

                window.addEventListener('touchend', finishTouch);
                window.addEventListener('touchcancel', finishTouch);
            }

            handle.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    toggleSheetState();
                }
            });
        }

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

        // Toggle GPS filter (show/hide non-GPS runs)
        function toggleGpsFilter() {
            showNonGpsRuns = !showNonGpsRuns;
            const btn = document.getElementById('gpsFilterBtn');
            if (showNonGpsRuns) {
                btn.textContent = 'Show all runs';
                btn.setAttribute('aria-pressed', 'false');
            } else {
                btn.textContent = 'Show GPS only';
                btn.setAttribute('aria-pressed', 'true');
            }
            // Re-render with current filter applied
            filterDrives(document.getElementById('searchInput').value);
        }

        function toggleCompactMode() {
            isCompactMode = !isCompactMode;
            const btn = document.getElementById('compactModeBtn');
            document.body.classList.toggle('compact-mode', isCompactMode);
            if (btn) {
                btn.textContent = isCompactMode ? 'Standard view' : 'Compact view';
                btn.setAttribute('aria-pressed', isCompactMode ? 'true' : 'false');
            }
        }

        function updateLegendBubbleUI() {
            const mapInfo = document.getElementById('mapInfo');
            const toggleBtn = document.getElementById('legendToggleBtn');
            const statusSpan = document.getElementById('legendBubbleStatus');
            if (!mapInfo || !toggleBtn || !statusSpan) return;

            if (isLegendCollapsed) {
                mapInfo.classList.add('collapsed');
                statusSpan.textContent = 'Stats';
                toggleBtn.setAttribute('aria-expanded', 'false');
                document.getElementById('driveInfoContent')?.setAttribute('aria-hidden', 'true');
                document.getElementById('trackLegend')?.setAttribute('aria-hidden', 'true');
            } else {
                mapInfo.classList.remove('collapsed');
                statusSpan.textContent = 'Stats';
                toggleBtn.setAttribute('aria-expanded', 'true');
                document.getElementById('driveInfoContent')?.setAttribute('aria-hidden', 'false');
                document.getElementById('trackLegend')?.setAttribute('aria-hidden', 'false');
            }
        }

        function toggleLegendBubble() {
            isLegendCollapsed = !isLegendCollapsed;
            updateLegendBubbleUI();
        }

        function updateDrawerToggleButton() {
            const btn = document.getElementById('drawerToggle');
            if (!btn) return;

            if (isMobile) {
                const collapsed = sheetState === 'collapsed';
                btn.textContent = collapsed ? '^' : 'v';
                btn.setAttribute('aria-pressed', collapsed ? 'false' : 'true');
                const label = collapsed ? 'Expand drive list' : 'Collapse drive list';
                btn.setAttribute('aria-label', label);
                btn.title = label;
                return;
            }

            btn.textContent = isDrawerCollapsed ? '>' : '<';
            btn.setAttribute('aria-pressed', isDrawerCollapsed ? 'true' : 'false');
            const label = isDrawerCollapsed ? 'Expand drive list' : 'Collapse drive list';
            btn.setAttribute('aria-label', label);
            btn.title = label;
        }

        function toggleDrawer() {
            if (isMobile) {
                if (sheetState === 'collapsed') {
                    setSheetState('half');
                } else {
                    setSheetState('collapsed');
                }
                return;
            }
            isDrawerCollapsed = !isDrawerCollapsed;
            document.body.classList.toggle('drawer-collapsed', isDrawerCollapsed);
            updateDrawerToggleButton();
        }

        // Filter drives by GPS data and search term
        function getFilteredDrives(allDrives, searchTerm = '') {
            let filtered = allDrives;

            // Apply GPS filter
            if (!showNonGpsRuns) {
                filtered = filtered.filter(drive => drive.stats.gps_samples > 0);
            }

            // Apply search filter
            if (searchTerm) {
                const term = searchTerm.toLowerCase();
                filtered = filtered.filter(drive =>
                    drive.datetime.toLowerCase().includes(term) ||
                    drive.id.toLowerCase().includes(term) ||
                    drive.stats.distance_km.toString().includes(term) ||
                    drive.stats.gps_samples.toString().includes(term)
                );
            }

            return filtered;
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

        // Fetch and display drives list (with server-side pagination)
        async function loadDrives() {
            try {
                const response = await fetch('/api/drives?limit=20&offset=0');
                const data = await response.json();
                drives = data.drives;
                window.totalDrives = data.total;
                window.hasMore = data.hasMore;

                if (drives.length === 0) {
                    document.getElementById('drivesList').innerHTML =
                        '<div class="error">No drives found. Start a tracking session!</div>';
                    return;
                }

                // Apply default filter (GPS only)
                const filtered = getFilteredDrives(drives, '');
                renderDrivesList(filtered, true);  // Reset pagination and show first batch

                // Auto-select first drive if it has GPX
                const firstWithGpx = filtered.find(d => d.has_gpx);
                if (firstWithGpx) {
                    selectDrive(firstWithGpx.id);
                }
            } catch (error) {
                console.error('Error loading drives:', error);
                document.getElementById('drivesList').innerHTML =
                    `<div class="error">Error loading drives: ${error.message}</div>`;
            }
        }

        // Group drives by time period
        function groupDrivesByTime(drivesToGroup) {
            const now = new Date();
            const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
            const weekAgo = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);
            const monthAgo = new Date(today.getFullYear(), today.getMonth(), 1);

            const groups = { today: [], week: [], month: [], older: [] };

            drivesToGroup.forEach(drive => {
                const driveDate = new Date(drive.timestamp);
                const driveDay = new Date(driveDate.getFullYear(), driveDate.getMonth(), driveDate.getDate());

                if (driveDay.getTime() === today.getTime()) {
                    groups.today.push(drive);
                } else if (driveDate >= weekAgo) {
                    groups.week.push(drive);
                } else if (driveDate >= monthAgo) {
                    groups.month.push(drive);
                } else {
                    groups.older.push(drive);
                }
            });

            return groups;
        }

        // Render drives list with grouping and filtering (paginated)
        function renderDrivesList(filteredDrives = null, reset = false) {
            const list = document.getElementById('drivesList');
            const drivesToRender = filteredDrives || drives;

            if (reset) {
                currentPage = 0;
                displayedDrives = [];
            }

            if (drivesToRender.length === 0) {
                list.innerHTML = '<div class="error">No drives found matching your search.</div>';
                return;
            }

            // Load first batch if empty
            if (displayedDrives.length === 0) {
                displayedDrives = drivesToRender.slice(0, DRIVES_PER_PAGE);
                currentPage = 1;
            }

            const groups = groupDrivesByTime(displayedDrives);
            let html = '';

            const renderGroup = (groupName, groupLabel, groupDrives) => {
                if (groupDrives.length === 0) return '';
                return `
                    <li class="group-header">${groupLabel}</li>
                    ${groupDrives.map(drive => `
                        <li class="drive-item" data-drive-id="${drive.id}" onclick="selectDrive('${drive.id}')" role="button" aria-selected="false" tabindex="0">
                            <div class="drive-date">${drive.datetime}</div>
                            <div class="drive-stats">
                                ${drive.stats.distance_km ? `<span class="stat-badge">${drive.stats.distance_km} km</span>` : ''}
                                <span class="stat-badge">${drive.stats.gps_samples} GPS</span>
                                <span class="stat-badge">${Math.round(drive.file_size_mb)} MB</span>
                                ${drive.has_gpx ? '<span class="stat-badge" style="color:#4caf50;">✓ GPS</span>' : '<span class="stat-badge" style="opacity:0.5;">✗ GPS</span>'}
                            </div>
                        </li>
                    `).join('')}
                `;
            };

            html += renderGroup('today', '📅 Today', groups.today);
            html += renderGroup('week', '📆 This Week', groups.week);
            html += renderGroup('month', '📊 This Month', groups.month);
            html += renderGroup('older', '📦 Older', groups.older);

            // Add load more button if there are more drives
            if (displayedDrives.length < drivesToRender.length) {
                html += `<li style="padding:15px 20px; text-align:center; border-top:1px solid var(--border-color);">
                    <button onclick="loadMoreDrives()" style="background:var(--accent-color); color:white; border:none; padding:10px 20px; border-radius:6px; cursor:pointer; font-weight:600; width:100%; max-width:200px;">
                        Load More (${displayedDrives.length}/${drivesToRender.length})
                    </button>
                </li>`;
            }

            list.innerHTML = html || '<div class="error">No drives found.</div>';
        }

        // Load more drives (fetch from API)
        async function loadMoreDrives() {
            if (isLoadingMore) return;
            isLoadingMore = true;

            try {
                const offset = displayedDrives.length;
                const response = await fetch(`/api/drives?limit=20&offset=${offset}`);
                const data = await response.json();

                if (data.drives && data.drives.length > 0) {
                    drives = drives.concat(data.drives);
                    displayedDrives = drives.slice(0, displayedDrives.length + DRIVES_PER_PAGE);
                    window.hasMore = data.hasMore;
                    renderDrivesList(null, false);
                }
            } catch (error) {
                console.error('Error loading more drives:', error);
            } finally {
                isLoadingMore = false;
            }
        }

        // Filter drives based on search input and GPS filter state
        function filterDrives(searchTerm) {
            const filtered = getFilteredDrives(drives, searchTerm || '');
            renderDrivesList(filtered, true);  // Reset pagination
        }

        // Select and display a drive
        async function selectDrive(driveId) {
            // Update UI with ARIA attributes
            document.querySelectorAll('.drive-item').forEach(item => {
                item.classList.remove('active');
                item.setAttribute('aria-selected', 'false');
            });
            const selected = document.querySelector(`.drive-item[data-drive-id="${driveId}"]`);
            if (selected) {
                selected.classList.add('active');
                selected.setAttribute('aria-selected', 'true');
            }

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
                    Peak Memory: ${drive.stats.peak_memory_mb} MB<br><br>
                    <div style="display:flex; gap:8px; flex-wrap:wrap;">
                        <button onclick="exportDrive('${driveId}', 'gpx')" style="flex:1; padding:6px; border:1px solid #667eea; background:#667eea; color:white; border-radius:4px; cursor:pointer; font-size:12px;">📍 GPX</button>
                        <button onclick="exportDrive('${driveId}', 'json')" style="flex:1; padding:6px; border:1px solid #667eea; background:#667eea; color:white; border-radius:4px; cursor:pointer; font-size:12px;">📋 JSON</button>
                    </div>
            `;
            mapInfo.style.display = 'block';
            isLegendCollapsed = true;
            updateLegendBubbleUI();

                // Load GPX if available
                if (drive.has_gpx) {
                    console.log('Drive has GPX - loading map...');
                    loadGpxOnMap(driveId);
                } else {
                    console.log('Drive has no GPX data');
                    showStatus('No GPS data available for this drive', 'error', 5000);
                    currentGpxLayers.forEach(layer => map.removeLayer(layer));
                    currentGpxLayers = [];
                    trackLayers = {};
                    trackVisibility = {};
                    const legend = document.getElementById('trackLegend');
                    if (legend) {
                        legend.innerHTML = '<div class="track-toggle">No tracks available</div>';
                    }
                }
            } catch (error) {
                console.error('Error loading drive details:', error);
                showStatus(`Error loading drive: ${error.message}`, 'error', 10000);
            }
        }

        // Export drive in different formats with loading state
        function exportDrive(driveId, format) {
            const drive = drives.find(d => d.id === driveId);
            if (!drive) return;

            const filename = `${driveId}.${format === 'gpx' ? 'gpx' : 'json'}`;
            const buttons = document.querySelectorAll(`button[onclick*="exportDrive('${driveId}', '${format}')"]`);

            // Disable buttons and show loading state
            buttons.forEach(btn => {
                btn.disabled = true;
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
            });

            const cleanup = () => {
                buttons.forEach(btn => {
                    btn.disabled = false;
                    btn.style.opacity = '1';
                    btn.style.cursor = 'pointer';
                });
            };

            if (format === 'gpx') {
                // Fetch and download GPX
                fetch(`/api/drive/${driveId}/gpx`)
                    .then(res => res.text())
                    .then(gpxText => {
                        const blob = new Blob([gpxText], {type: 'application/gpx+xml'});
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = filename;
                        a.click();
                        URL.revokeObjectURL(url);
                        showStatus('GPX exported successfully!', 'success', 2000);
                    })
                    .catch(err => showStatus(`Export failed: ${err.message}`, 'error', 5000))
                    .finally(cleanup);
            } else if (format === 'json') {
                // Fetch and download JSON
                fetch(`/api/drive/${driveId}`)
                    .then(res => res.json())
                    .then(data => {
                        const jsonText = JSON.stringify(data, null, 2);
                        const blob = new Blob([jsonText], {type: 'application/json'});
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = filename;
                        a.click();
                        URL.revokeObjectURL(url);
                        showStatus('JSON exported successfully!', 'success', 2000);
                    })
                    .catch(err => showStatus(`Export failed: ${err.message}`, 'error', 5000))
                    .finally(cleanup);
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

                // Remove previous layers
                currentGpxLayers.forEach(layer => map.removeLayer(layer));
                currentGpxLayers = [];
                trackLayers = {};
                trackVisibility = {};

                // Parse GPX
                showStatus('Parsing GPS data...', 'info', 0);
                const geoJSON = gpxToGeoJSON(gpxText);
                console.log('GeoJSON features:', geoJSON.features.length);

                const trackFeatures = {};
                const trackHasRoute = {};
                geoJSON.features.forEach(feature => {
                    const trackName = feature.properties.trackName || 'GPS';
                    if (!trackFeatures[trackName]) trackFeatures[trackName] = [];
                    if (!trackHasRoute[trackName]) trackHasRoute[trackName] = false;
                    if (feature.geometry?.type === 'LineString' && feature.geometry.coordinates?.length > 1) {
                        trackHasRoute[trackName] = true;
                    }
                    trackFeatures[trackName].push(feature);
                });

                const trackNames = Object.keys(trackFeatures);
                showStatus(`Parsed ${trackNames.length} track(s)`, 'info', 2000);

                const primaryTrack = TRACK_PRIORITY.find(name => trackHasRoute[name]) ||
                                     trackNames.find(name => trackHasRoute[name]) ||
                                     trackNames[0];
                trackVisibility = {};
                trackNames.forEach(name => {
                    trackVisibility[name] = name === primaryTrack;
                });

                trackNames.forEach(name => {
                    const layer = L.geoJSON({type: 'FeatureCollection', features: trackFeatures[name]}, {
                        style: (feature) => {
                            if (feature.geometry.type === 'LineString') {
                                return {
                                    color: TRACK_COLORS[name] || '#667eea',
                                    weight: 3,
                                    opacity: 0.85,
                                };
                            }
                            return {color: TRACK_COLORS[name] || '#667eea'};
                        },
                        pointToLayer: (feature, latlng) => {
                            const color = TRACK_COLORS[name] || '#667eea';
                            if (feature.properties.type === 'start' || feature.properties.type === 'end') {
                                return L.circleMarker(latlng, {
                                    radius: 6,
                                    fillColor: color,
                                    color: '#fff',
                                    weight: 2,
                                    opacity: 1,
                                    fillOpacity: 0.85,
                                });
                            }
                            return L.circleMarker(latlng, {radius: 2, color, opacity: 0.9, fillOpacity: 0.9});
                        },
                    });

                    trackLayers[name] = layer;
                    if (!(name in trackVisibility)) {
                        trackVisibility[name] = Object.prototype.hasOwnProperty.call(DEFAULT_TRACK_VISIBILITY, name)
                            ? DEFAULT_TRACK_VISIBILITY[name]
                            : true;
                    }
                    if (trackVisibility[name]) {
                        layer.addTo(map);
                        currentGpxLayers.push(layer);
                    }
                });

                updateTrackLegend(trackNames);

                let combinedBounds = null;
                Object.entries(trackLayers).forEach(([name, layer]) => {
                    if (trackVisibility[name] === false) return;
                    const bounds = layer.getBounds();
                    if (bounds.isValid()) {
                        combinedBounds = combinedBounds ? combinedBounds.extend(bounds) : bounds;
                    }
                });

                if (combinedBounds && combinedBounds.isValid()) {
                    showStatus('Rendering map...', 'info', 0);
                    map.fitBounds(combinedBounds, {padding: [50, 50]});
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

        function updateTrackLegend(trackNames) {
            const legend = document.getElementById('trackLegend');
            if (!legend) return;
            if (!trackNames || trackNames.length === 0) {
                legend.innerHTML = '<div class=\"track-toggle\">No tracks available</div>';
                return;
            }

            trackNames.forEach(name => {
                if (!(name in trackVisibility)) {
                    trackVisibility[name] = Object.prototype.hasOwnProperty.call(DEFAULT_TRACK_VISIBILITY, name)
                        ? DEFAULT_TRACK_VISIBILITY[name]
                        : true;
                }
            });

            legend.innerHTML = trackNames.map(name => `
                <label class=\"track-toggle\">
                    <input type=\"checkbox\" data-track=\"${name}\" ${trackVisibility[name] ? 'checked' : ''}>
                    <span class=\"track-color\" style=\"background:${TRACK_COLORS[name] || '#667eea'}\"></span>
                    <span>${name}</span>
                </label>
            `).join('');

            legend.querySelectorAll('input').forEach(input => {
                input.addEventListener('change', (event) => {
                    const track = event.target.dataset.track;
                    const visible = event.target.checked;
                    trackVisibility[track] = visible;
                    const layer = trackLayers[track];
                    if (!layer) return;
                    if (visible) {
                        layer.addTo(map);
                    } else {
                        map.removeLayer(layer);
                    }
                });
            });
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

            const features = [];
            const tracks = Array.from(xmlDoc.getElementsByTagName('trk'));
            console.log(`Found ${tracks.length} track(s) in GPX`);

            tracks.forEach((trk, trackIndex) => {
                const nameEl = trk.getElementsByTagName('name')[0];
                const trackName = nameEl ? nameEl.textContent.trim() : `Track ${trackIndex + 1}`;

                const pts = Array.from(trk.getElementsByTagName('trkpt'));
                const coords = [];

                pts.forEach((pt, idx) => {
                    const lat = pt.getAttribute('lat');
                    const lon = pt.getAttribute('lon');
                    if (lat === null || lon === null) {
                        console.warn(`[${trackName}] Sample ${idx}: Missing lat/lon`);
                        return;
                    }
                    const latNum = parseFloat(lat);
                    const lonNum = parseFloat(lon);
                    if (isNaN(latNum) || isNaN(lonNum)) {
                        console.warn(`[${trackName}] Sample ${idx}: Invalid numbers`);
                        return;
                    }
                    if (latNum < -90 || latNum > 90 || lonNum < -180 || lonNum > 180) {
                        console.warn(`[${trackName}] Sample ${idx}: Out of range lat/lon`);
                        return;
                    }
                    coords.push([lonNum, latNum]);
                });

                if (coords.length === 0) {
                    console.warn(`[${trackName}] Track has no valid coordinates`);
                    return;
                }

                features.push({
                    type: 'Feature',
                    properties: {type: 'start', trackName},
                    geometry: {type: 'Point', coordinates: coords[0]}
                });
                features.push({
                    type: 'Feature',
                    properties: {type: 'route', trackName},
                    geometry: {type: 'LineString', coordinates: coords}
                });
                features.push({
                    type: 'Feature',
                    properties: {type: 'end', trackName},
                    geometry: {type: 'Point', coordinates: coords[coords.length - 1]}
                });
            });

            if (features.length === 0) {
                throw new Error('No valid tracks found in GPX');
            }

            return {
                type: 'FeatureCollection',
                features
            };
        }

        // Debounce helper for search input
        function debounce(func, delay) {
            let timeoutId;
            return function(...args) {
                clearTimeout(timeoutId);
                timeoutId = setTimeout(() => func(...args), delay);
            };
        }

        const debouncedFilter = debounce(filterDrives, 300);

        // Initialize on page load
        window.addEventListener('DOMContentLoaded', () => {
            initTheme();  // Initialize theme
            initMap();
            loadDrives();
            initSheetDrag();  // Initialize bottom sheet for mobile
            applySheetHeightFromState();
            if (isMobile) {
                baselineMobileHeight = getViewportHeight();
            }

            // Add search input listener with debouncing
            const searchInput = document.getElementById('searchInput');
            searchInput.addEventListener('input', (e) => {
                debouncedFilter(e.target.value);
            });

            // Add GPS filter button listener
            const gpsFilterBtn = document.getElementById('gpsFilterBtn');
            if (gpsFilterBtn) {
                gpsFilterBtn.addEventListener('click', toggleGpsFilter);
            }

            const compactModeBtn = document.getElementById('compactModeBtn');
            if (compactModeBtn) {
                compactModeBtn.addEventListener('click', toggleCompactMode);
            }

            const drawerToggle = document.getElementById('drawerToggle');
            if (drawerToggle) {
                drawerToggle.addEventListener('click', toggleDrawer);
            }

            const legendToggleBtn = document.getElementById('legendToggleBtn');
            if (legendToggleBtn) {
                legendToggleBtn.addEventListener('click', toggleLegendBubble);
            }

            updateDrawerToggleButton();
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
