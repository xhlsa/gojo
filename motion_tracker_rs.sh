#!/bin/bash
# Rust Motion Tracker - EKF vs Complementary filter comparison
# Usage: ./motion_tracker_rs.sh [DURATION] [OPTIONS]
# Examples:
#   ./motion_tracker_rs.sh 5                    # 5 minute test
#   ./motion_tracker_rs.sh 10 --enable-gyro     # With gyroscope
#   ./motion_tracker_rs.sh                      # Continuous mode

set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT/motion_tracker_rs"

# Build if needed
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

# Run the binary
./target/release/motion_tracker "${converted_args[@]}"
