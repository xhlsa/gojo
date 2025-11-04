#!/bin/bash
# ⚠️  MANDATORY: SHELL SCRIPT FOR SENSOR INITIALIZATION
#
# Test EKF vs Complementary Filter - Real-time comparison with ROBUST sensor initialization
# WITH CRASH LOGGING AND SESSION TRACKING
#
# Usage (CORRECT):
#   ./test_ekf.sh 10              # Run 10-minute test (gyro enabled by default)
#   ./test_ekf.sh 5               # 5 minutes with gyroscope (always enabled)
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
#   6. Crash logging with context preservation
#
# METRICS CAPTURED (always):
#   Real-time filter comparison (EKF vs Complementary)
#   Distance accuracy vs GPS ground truth
#   Velocity smoothness analysis
#   Gyroscope data collection status
#   Memory usage and sensor health
#   Results saved to: motion_tracker_sessions/comparison_*.json
#   Analysis: python3 motion_tracker_v2/analyze_comparison.py comparison_*.json
#
# CRASH ANALYSIS:
#   Show recent crashes: python3 crash_logger.py show
#   All logs in: crash_logs/
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

# Initialize crash logging
CRASH_LOGGER_FILE="crash_logs/active_session.log"
mkdir -p crash_logs
LOG_FILE="crash_logs/test_ekf_$(date +%Y-%m-%d_%H-%M-%S).log"

# Log function that writes to both stdout and crash log
log_event() {
    local msg="$1"
    echo "$msg" | tee -a "$LOG_FILE" >&2
}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Cleanup function - kills ONLY sensor-related processes (NOT GPS/Location API)
cleanup_sensors() {
    echo -e "${YELLOW}Cleaning up sensor processes...${NC}" >&2
    echo "[cleanup_sensors] Starting" >> "$LOG_FILE" 2>/dev/null || true

    # Kill ONLY sensor wrapper processes (updated to match new LSM6DSO-specific sensor names)
    # Both old generic names and new specific names for backwards compatibility
    pkill -9 -f "termux-sensor.*ACCELEROMETER" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*Accelerometer" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*GYROSCOPE" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*Gyroscope" 2>/dev/null || true

    # Kill sensor backend SPECIFICALLY (pattern MUST include "Sensor" to avoid matching "Location")
    # Original pattern "termux-api-broadcast.*Sensor" was too broad and killed GPS backend
    pkill -9 -f "termux-api Sensor" 2>/dev/null || true

    # Kill stdbuf wrapper processes that may be running sensor commands
    pkill -9 -f "stdbuf.*termux-sensor" 2>/dev/null || true

    # Also kill any stale Python test processes that might be holding sensors
    pkill -9 -f "test_ekf_vs_complementary.py" 2>/dev/null || true

    # Extended delay: Android needs time to fully release sensor HAL resources
    # 5 seconds is required for reliable cleanup and daemon restart
    sleep 5

    echo -e "${GREEN}✓ Sensor cleanup complete${NC}" >&2
    echo "[cleanup_sensors] Complete" >> "$LOG_FILE" 2>/dev/null || true
}

# Cleanup function - kills GPS-related processes (separate from sensors)
# REASON: GPS backend (termux-api Location) socket exhaustion causes "Connection refused" errors
# Each termux-location call opens a socket that doesn't get released without explicit cleanup
cleanup_gps() {
    echo -e "${YELLOW}Cleaning up GPS processes...${NC}" >&2
    echo "[cleanup_gps] Starting" >> "$LOG_FILE" 2>/dev/null || true

    # Kill termux-location wrapper processes
    pkill -9 -f "termux-location" 2>/dev/null || true

    # CRITICAL: Kill GPS backend process (NOT Sensor, specifically Location)
    # Root cause of "Connection refused" error: these processes accumulate sockets
    pkill -9 -f "termux-api Location" 2>/dev/null || true

    # Extended delay: Android LocationManager needs time to fully release socket resources
    # Same 5-second delay as accelerometer, applied to GPS backend
    sleep 5

    echo -e "${GREEN}✓ GPS cleanup complete${NC}" >&2
    echo "[cleanup_gps] Complete" >> "$LOG_FILE" 2>/dev/null || true
}

# Pre-flight validation - verify accelerometer is accessible
validate_sensor() {
    echo -e "${YELLOW}Validating accelerometer access...${NC}"

    # Try to get TWO samples from accelerometer with 5-second timeout
    # First sample is often empty {}, second has real data
    # Use $HOME instead of /tmp for Termux permissions
    # Using specific LSM6DSO sensor ID for reliable activation
    if timeout 5 termux-sensor -s "lsm6dso LSM6DSO Accelerometer Non-wakeup" -d 50 -n 2 > "$HOME/.sensor_test.json" 2>&1; then
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

# Validate GPS API service (warn but don't fail if unavailable)
validate_gps_api() {
    echo -e "${YELLOW}Validating GPS API service...${NC}"

    # Quick GPS test with timeout
    if timeout 10 termux-location -p gps > "$HOME/.gps_test.json" 2>&1; then
        # Check for valid GPS data (latitude field indicates success)
        if grep -q "latitude" "$HOME/.gps_test.json" 2>/dev/null; then
            echo -e "${GREEN}✓ GPS API responding correctly${NC}"
            rm -f "$HOME/.gps_test.json"
            return 0
        fi
    fi

    echo -e "${YELLOW}⚠ GPS API not responding (test will continue with accelerometer only)${NC}"
    rm -f "$HOME/.gps_test.json"
    return 1  # Non-fatal warning
}

# Retry mechanism for GPS initialization (matches accelerometer retry pattern)
initialize_gps_with_retry() {
    local max_attempts=3
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        echo -e "\n${YELLOW}GPS initialization attempt $attempt/$max_attempts${NC}"

        # Clean up GPS backend before attempting connection
        cleanup_gps

        # Try to validate GPS
        if validate_gps_api; then
            return 0
        fi

        if [ $attempt -lt $max_attempts ]; then
            echo -e "${YELLOW}Retrying in 3 seconds...${NC}"
            sleep 3
        fi

        attempt=$((attempt + 1))
    done

    # Non-fatal: GPS optional, test continues without it
    echo -e "${YELLOW}⚠ GPS unavailable after $max_attempts attempts, test will continue with accelerometer only${NC}"
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
    local signal_name=""

    # Detect signal number from exit code (128 + signal_num)
    if [ $exit_code -gt 128 ]; then
        signal_num=$((exit_code - 128))
        case $signal_num in
            9) signal_name="SIGKILL (9)" ;;
            15) signal_name="SIGTERM (15)" ;;
            2) signal_name="SIGINT (2)" ;;
            *) signal_name="Signal $signal_num" ;;
        esac
    fi

    echo -e "\n${YELLOW}Test finished, performing final cleanup...${NC}" | tee -a "$LOG_FILE"

    # Kill Python test process if still running
    if [ ! -z "$TEST_PID" ]; then
        kill -TERM "$TEST_PID" 2>/dev/null || true
        wait "$TEST_PID" 2>/dev/null || true
    fi

    # Final sensor cleanup
    cleanup_sensors

    # Log crash information
    if [ $exit_code -ne 0 ]; then
        {
            echo "==================================================================="
            echo "CRASH/ERROR DETECTED"
            echo "==================================================================="
            echo "Exit code: $exit_code"
            if [ ! -z "$signal_name" ]; then
                echo "Signal: $signal_name"
            fi
            echo "Test: test_ekf.sh $@"
            echo "Timestamp: $(date -u)"
            echo "Log file: $LOG_FILE"
            echo ""
            echo "LAST 50 LINES OF OUTPUT:"
            tail -50 "$LOG_FILE" 2>/dev/null || echo "(Log file unavailable)"
            echo "==================================================================="
        } | tee -a "$LOG_FILE"

        # Print to stdout for visibility
        echo -e "\n${RED}✗ TEST CRASHED - Exit code $exit_code${NC}"
        if [ ! -z "$signal_name" ]; then
            echo -e "${RED}  Signal: $signal_name${NC}"
        fi
        echo -e "${RED}  Log: $LOG_FILE${NC}"
        echo -e "${RED}  View all crashes: python3 crash_logger.py show${NC}"
    else
        echo -e "${GREEN}✓ Test completed successfully${NC}" | tee -a "$LOG_FILE"
    fi

    exit $exit_code
}

# Register cleanup handler
trap cleanup_on_exit EXIT SIGINT SIGTERM

# Main execution
{
    echo "=============================================================================="
    echo "EKF vs Complementary Filter Test - Robust Sensor Initialization"
    echo "Session started: $(date -u)"
    echo "Test arguments: $@"
    echo "Log file: $LOG_FILE"
    echo "=============================================================================="
} | tee "$LOG_FILE"

# Step 1: Initialize sensor with retry
if ! initialize_sensor_with_retry 2>&1 | tee -a "$LOG_FILE"; then
    exit 1
fi

# Step 1.5: Initialize GPS with retry (non-fatal if GPS unavailable)
initialize_gps_with_retry 2>&1 | tee -a "$LOG_FILE"
# GPS is optional - test continues regardless of result

# Step 2: Brief pause to ensure sensor resources are stable
echo -e "\n${GREEN}✓ Sensor ready, starting test in 2 seconds...${NC}" | tee -a "$LOG_FILE"
sleep 2

# Step 3: Launch Python test process in background
echo -e "${GREEN}Starting Python test...${NC}\n" | tee -a "$LOG_FILE"

# Capture both stdout and stderr from Python process
# Enable gyro by default (since we now have GPS and accel data flowing reliably)
python3 motion_tracker_v2/test_ekf_vs_complementary.py --enable-gyro "$@" 2>&1 | tee -a "$LOG_FILE" &
TEST_PID=$!

# Step 4: Monitor Python process
# Wait for process to complete naturally
wait $TEST_PID
exit_code=$?

# Cleanup will be handled by trap
exit $exit_code
