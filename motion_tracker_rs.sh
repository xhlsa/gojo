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

# Run the binary
./target/release/motion_tracker "$@"
