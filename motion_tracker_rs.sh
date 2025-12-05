#!/bin/bash
# Rust Motion Tracker - EKF vs Complementary filter comparison
# Usage: ./motion_tracker_rs.sh [DURATION] [OPTIONS]
# Examples:
#   ./motion_tracker_rs.sh 5                    # 5 minute test
#   ./motion_tracker_rs.sh 10 --enable-gyro     # With gyroscope
#   ./motion_tracker_rs.sh                      # Continuous mode

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/motion_tracker_sessions"
mkdir -p "$OUTPUT_DIR"

# ============================================================================
# SENSOR CLEANUP (from test_ekf.sh pattern)
# ============================================================================
cleanup_sensors() {
    echo "[$(date '+%H:%M:%S')] Cleaning up stale sensor processes..." >&2
    pkill -9 -f "termux-sensor.*ACCELEROMETER" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*Accelerometer" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*GYROSCOPE" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*Gyroscope" 2>/dev/null || true
    pkill -9 -f "termux-api Sensor" 2>/dev/null || true
    pkill -9 -f "stdbuf.*termux-sensor" 2>/dev/null || true
    pkill -9 -f "test_ekf_vs_complementary.py" 2>/dev/null || true
    sleep 5  # Android needs time to release sensor HAL
    echo "[$(date '+%H:%M:%S')] ✓ Sensor cleanup complete" >&2
}

cleanup_gps() {
    echo "[$(date '+%H:%M:%S')] Cleaning up stale GPS processes..." >&2
    pkill -9 -f "termux-location" 2>/dev/null || true
    pkill -9 -f "termux-api Location" 2>/dev/null || true
    sleep 5  # Android needs time to release GPS socket resources
    echo "[$(date '+%H:%M:%S')] ✓ GPS cleanup complete" >&2
}

cleanup_on_exit() {
    local exit_code=$?
    echo "[$(date '+%H:%M:%S')] Performing final cleanup..." >&2
    cleanup_sensors
    cleanup_gps
    echo "[$(date '+%H:%M:%S')] Releasing wakelock..." >&2
    termux-wake-unlock
    exit $exit_code
}

trap cleanup_on_exit EXIT SIGINT SIGTERM

# ============================================================================
# PRE-RUN CLEANUP
# ============================================================================
echo "[$(date '+%H:%M:%S')] Acquiring wakelock..." >&2
termux-wake-lock

cleanup_sensors
cleanup_gps

# Build if needed
cd "$SCRIPT_DIR/motion_tracker_rs"
if [ ! -f target/release/motion_tracker ]; then
    echo "[$(date '+%H:%M:%S')] Building Rust binary..."
    cargo build --release --bin motion_tracker 2>&1 | grep -E "(Compiling|Finished|error)" || true
fi

# Convert legacy minute argument to seconds for Rust binary (expects seconds)
converted_args=()
if [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; then
    minutes="$1"
    shift
    if [ "$minutes" -eq 0 ]; then
        converted_args+=(0)
    else
        converted_args+=($((minutes * 60)))
    fi
fi

converted_args+=("$@")

# Run the binary with explicit output directory (positional args BEFORE flags in clap)
./target/release/motion_tracker "${converted_args[@]}" --output-dir "$OUTPUT_DIR"
