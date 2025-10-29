#!/bin/bash
# Motion Tracker V2 launcher - runs the tracker with proper signal handling
#
# Usage:
#   ./motion_tracker_v2.sh [duration_minutes]                   # Run with default EKF filter
#   ./motion_tracker_v2.sh --filter=complementary 5             # Run complementary filter for 5 minutes
#   ./motion_tracker_v2.sh --filter=ekf --enable-gyro 10        # Run EKF with gyro for 10 minutes
#   ./motion_tracker_v2.sh --test                               # Quick 2-minute test run
#
# Options:
#   --filter=FILTER     Choose filter: 'complementary', 'kalman', or 'ekf' (default: ekf)
#   --enable-gyro       Enable gyroscope support in EKF filter (orientation tracking)
#   --gyro              Alias for --enable-gyro
#   --test              Run 2-minute test
#   20                  Set accel sampling rate (Hz, default: 20)
#   5                   Duration in minutes (if numeric)

set -e  # Exit on errors

# Get directory and change to it
SCRIPT_DIR="$(dirname "$0")"
cd "$SCRIPT_DIR" || exit 1

# Launch Python process and capture PID
python motion_tracker_v2/motion_tracker_v2.py "$@" &
TRACKER_PID=$!

# Forward signals to Python process
trap "kill -TERM $TRACKER_PID 2>/dev/null || true; wait $TRACKER_PID 2>/dev/null || true" SIGINT SIGTERM

# Wait for process and return its exit code
wait $TRACKER_PID
exit_code=$?

exit $exit_code
