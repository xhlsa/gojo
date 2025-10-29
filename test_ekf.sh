#!/bin/bash
# Test EKF vs Complementary Filter - Real-time comparison with proper sensor initialization
#
# Usage:
#   ./test_ekf.sh 10              # Run 10-minute test
#   ./test_ekf.sh 5 --gyro        # 5 minutes with gyroscope
#
# This script properly initializes the sensor environment and manages cleanup

set -e

# Get directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Kill any lingering sensor processes from previous runs
echo "Cleaning up sensor processes..."
pkill -9 -f "termux-sensor" 2>/dev/null || true
pkill -9 -f "termux-api.*Sensor" 2>/dev/null || true
sleep 3  # Give system time to fully release sensor resources

# Launch Python test process
echo "Starting test..."
python3 motion_tracker_v2/test_ekf_vs_complementary.py "$@" &
TEST_PID=$!

# Forward signals to Python process
trap "kill -TERM $TEST_PID 2>/dev/null || true; wait $TEST_PID 2>/dev/null || true" SIGINT SIGTERM

# Wait for process and capture exit code
wait $TEST_PID
exit_code=$?

# Final cleanup
echo "Cleaning up sensor processes..."
pkill -9 -f "termux-sensor" 2>/dev/null || true

exit $exit_code
