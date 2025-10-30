#!/bin/bash
# ⚠️  MANDATORY: SHELL SCRIPT FOR SENSOR INITIALIZATION
#
# Test EKF vs Complementary Filter - Real-time comparison with ROBUST sensor initialization
#
# Usage (CORRECT):
#   ./test_ekf.sh 10              # Run 10-minute test
#   ./test_ekf.sh 5 --gyro        # 5 minutes with gyroscope
#
# DO NOT use:
#   python motion_tracker_v2/test_ekf_vs_complementary.py   # ❌ WRONG - sensor fails
#
# This script ensures reliable accelerometer access by:
#   1. Comprehensive process cleanup (termux-sensor AND termux-api backend)
#   2. Extended delay for Android sensor resource release
#   3. Pre-flight sensor validation before starting Python
#   4. Proper signal handling and cleanup on exit
#   5. Retry mechanism if sensor not immediately available
#
# LESSONS LEARNED (Oct 29 session):
#   - Direct Python execution bypasses sensor initialization → fails
#   - Stale termux-sensor processes block accelerometer access
#   - Retry logic needed: first attempt often fails, second succeeds
#   - 5-second cleanup delay required for sensor HAL release
#   - Validation MUST check for JSON "values" field, not empty objects

set -e

# Get directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Cleanup function - kills ALL sensor-related processes
cleanup_sensors() {
    echo -e "${YELLOW}Cleaning up sensor processes...${NC}"

    # Kill termux-sensor wrapper processes
    pkill -9 -f "termux-sensor" 2>/dev/null || true

    # CRITICAL: Kill termux-api backend processes (the actual sensor daemons)
    pkill -9 -f "termux-api.*Sensor" 2>/dev/null || true
    pkill -9 -f "termux-api-broadcast.*Sensor" 2>/dev/null || true

    # Also kill any stale Python processes that might be holding sensors
    pkill -9 -f "test_ekf_vs_complementary.py" 2>/dev/null || true

    # Extended delay: Android needs time to fully release sensor HAL resources
    # 3 seconds was insufficient, 5 seconds is more reliable
    sleep 5

    echo -e "${GREEN}✓ Sensor cleanup complete${NC}"
}

# Pre-flight validation - verify accelerometer is accessible
validate_sensor() {
    echo -e "${YELLOW}Validating accelerometer access...${NC}"

    # Try to get TWO samples from accelerometer with 5-second timeout
    # First sample is often empty {}, second has real data
    # Use $HOME instead of /tmp for Termux permissions
    if timeout 5 termux-sensor -s ACCELEROMETER -d 50 -n 2 > "$HOME/.sensor_test.json" 2>&1; then
        # Check if we got valid JSON output with "values" field
        if grep -q "values" "$HOME/.sensor_test.json" 2>/dev/null; then
            echo -e "${GREEN}✓ Accelerometer responding correctly${NC}"
            rm -f "$HOME/.sensor_test.json"
            return 0
        fi
    fi

    echo -e "${RED}✗ Accelerometer validation failed${NC}"
    # Show what we got for debugging
    if [ -f "$HOME/.sensor_test.json" ]; then
        echo -e "${RED}Output received:${NC}"
        cat "$HOME/.sensor_test.json"
    fi
    rm -f "$HOME/.sensor_test.json"
    return 1
}

# Retry mechanism for sensor initialization
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
    echo -e "${RED}Troubleshooting steps:${NC}"
    echo -e "  1. Check sensor permissions: termux-sensor -l"
    echo -e "  2. Restart Termux app completely"
    echo -e "  3. Verify phone's accelerometer works in other apps"
    echo -e "  4. Check for conflicting apps using accelerometer"
    return 1
}

# Cleanup handler for script exit
cleanup_on_exit() {
    local exit_code=$?
    echo -e "\n${YELLOW}Test finished, performing final cleanup...${NC}"

    # Kill Python test process if still running
    if [ ! -z "$TEST_PID" ]; then
        kill -TERM "$TEST_PID" 2>/dev/null || true
        wait "$TEST_PID" 2>/dev/null || true
    fi

    # Final sensor cleanup
    cleanup_sensors

    exit $exit_code
}

# Register cleanup handler
trap cleanup_on_exit EXIT SIGINT SIGTERM

# Main execution
echo "=============================================================================="
echo "EKF vs Complementary Filter Test - Robust Sensor Initialization"
echo "=============================================================================="

# Step 1: Initialize sensor with retry
if ! initialize_sensor_with_retry; then
    exit 1
fi

# Step 2: Brief pause to ensure sensor resources are stable
echo -e "\n${GREEN}✓ Sensor ready, starting test in 2 seconds...${NC}"
sleep 2

# Step 3: Launch Python test process in background
echo -e "${GREEN}Starting Python test...${NC}\n"
python3 motion_tracker_v2/test_ekf_vs_complementary.py "$@" &
TEST_PID=$!

# Step 4: Monitor Python process
# Wait for process to complete naturally
wait $TEST_PID
exit_code=$?

# Cleanup will be handled by trap
exit $exit_code
