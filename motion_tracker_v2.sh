#!/bin/bash
# ⚠️  MANDATORY: SHELL SCRIPT FOR SENSOR INITIALIZATION
#
# Motion Tracker V2 launcher - runs the tracker with robust sensor management
#
# Usage:
#   ./motion_tracker_v2.sh 5                                     # Run for 5 minutes (default: EKF)
#   ./motion_tracker_v2.sh --filter=complementary 10             # Complementary filter, 10 minutes
#   ./motion_tracker_v2.sh --filter=ekf --enable-gyro 10         # EKF with gyroscope
#   ./motion_tracker_v2.sh --test                                # Quick 2-minute test
#
# Options:
#   --filter=FILTER     Choose: 'complementary', 'kalman', or 'ekf' (default: ekf)
#   --enable-gyro       Enable gyroscope in EKF (orientation tracking)
#   --gyro              Alias for --enable-gyro
#   --test              Quick 2-minute test run
#   [duration]          Minutes to run (if numeric)
#
# IMPORTANT NOTES:
#   - Always use this shell script, NOT direct Python execution
#   - Script handles sensor cleanup and initialization automatically
#   - Output saved to: motion_tracker_sessions/
#   - Data can be analyzed with: python analyze_comparison.py motion_tracker_sessions/*.json
#
# LESSONS LEARNED (Oct 29 session):
#   - Stale sensor processes must be cleaned before startup
#   - Sensor validation with retry logic needed
#   - 5-second delay required for Android sensor resource release
#   - Direct Python execution fails with sensor initialization
#   - Proper signal handling prevents zombie processes

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get directory and change to it
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Cleanup function - kills stale sensor processes
cleanup_sensors() {
    echo -e "${YELLOW}Cleaning up sensor processes...${NC}"

    # Kill stale processes (both old generic names and new LSM6DSO-specific names)
    pkill -9 -f "termux-sensor.*ACCELEROMETER" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*Accelerometer" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*GYROSCOPE" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*Gyroscope" 2>/dev/null || true
    pkill -9 -f "stdbuf.*termux-sensor" 2>/dev/null || true
    pkill -9 -f "termux-api Sensor" 2>/dev/null || true
    pkill -9 -f "termux-api Location" 2>/dev/null || true
    pkill -9 -f "motion_tracker_v2.py" 2>/dev/null || true

    # Wait for Android to release sensor resources (5 seconds required)
    sleep 5

    echo -e "${GREEN}✓ Sensor cleanup complete${NC}"
}

# Validate accelerometer is accessible
validate_sensor() {
    echo -e "${YELLOW}Validating accelerometer access...${NC}"

    # Use $HOME instead of /tmp for Termux permissions
    # Using specific LSM6DSO sensor ID for reliable activation
    if timeout 5 termux-sensor -s "lsm6dso LSM6DSO Accelerometer Non-wakeup" -d 50 -n 2 > "$HOME/.sensor_test.json" 2>&1; then
        if grep -q "values" "$HOME/.sensor_test.json" 2>/dev/null; then
            echo -e "${GREEN}✓ Accelerometer responding${NC}"
            rm -f "$HOME/.sensor_test.json"
            return 0
        fi
    fi

    echo -e "${RED}✗ Accelerometer validation failed${NC}"
    rm -f "$HOME/.sensor_test.json"
    return 1
}

# Initialize sensor with retry
initialize_sensor_with_retry() {
    local max_attempts=3
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        echo -e "\n${YELLOW}Sensor initialization attempt $attempt/$max_attempts${NC}"

        cleanup_sensors

        if validate_sensor; then
            return 0
        fi

        if [ $attempt -lt $max_attempts ]; then
            echo -e "${YELLOW}Retrying in 3 seconds...${NC}"
            sleep 3
        fi

        attempt=$((attempt + 1))
    done

    echo -e "\n${RED}ERROR: Failed to initialize accelerometer after $max_attempts attempts${NC}"
    echo -e "${RED}Troubleshooting:${NC}"
    echo -e "  1. Check sensor: termux-sensor -l"
    echo -e "  2. Restart Termux app completely"
    echo -e "  3. Verify phone accelerometer works in other apps"
    return 1
}

# Cleanup handler for script exit
cleanup_on_exit() {
    local exit_code=$?
    echo -e "\n${YELLOW}Tracker finished, performing final cleanup...${NC}"

    # Kill Python process if still running
    if [ ! -z "$TRACKER_PID" ]; then
        kill -TERM "$TRACKER_PID" 2>/dev/null || true
        wait "$TRACKER_PID" 2>/dev/null || true
    fi

    # Final sensor cleanup
    cleanup_sensors

    exit $exit_code
}

# Register cleanup handler
trap cleanup_on_exit EXIT SIGINT SIGTERM

# Main execution
echo "=============================================================================="
echo "Motion Tracker V2 - Sensor Fusion Incident Logger"
echo "=============================================================================="

# Step 1: Initialize sensor with retry
if ! initialize_sensor_with_retry; then
    exit 1
fi

# Step 2: Brief pause to ensure sensor resources are stable
echo -e "\n${GREEN}✓ Sensor ready, starting tracker in 2 seconds...${NC}"
sleep 2

# Step 3: Launch Python tracker
echo -e "${GREEN}Starting motion tracker...${NC}\n"
python motion_tracker_v2/motion_tracker_v2.py "$@" &
TRACKER_PID=$!

# Step 4: Monitor process
wait $TRACKER_PID
exit_code=$?

# Cleanup will be handled by trap
exit $exit_code
