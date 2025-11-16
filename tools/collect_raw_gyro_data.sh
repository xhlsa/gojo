#!/bin/bash
# Script to robustly collect raw gyroscope data using termux-sensor

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Log function that writes to both stdout and stderr
log_event() {
    local msg="$1"
    echo "$msg" >&2
}

# Cleanup function - kills ONLY sensor-related processes
cleanup_sensors() {
    log_event "${YELLOW}--- Starting Sensor Cleanup ---${NC}"
    
    log_event "Processes before cleanup:"
    ps -ef | grep -i "termux-sensor" | grep -v "grep" || log_event "No termux-sensor processes found."

    log_event "Killing all termux-sensor processes..."
    pkill -9 -f "termux-sensor" 2>/dev/null || true
    
    log_event "Killing all termux-api Sensor processes..."
    pkill -9 -f "termux-api Sensor" 2>/dev/null || true
    
    log_event "Killing all stdbuf processes wrapping termux-sensor..."
    pkill -9 -f "stdbuf.*termux-sensor" 2>/dev/null || true

    log_event "Waiting for 5 seconds for resources to be released..."
    sleep 5

    log_event "Processes after cleanup:"
    ps -ef | grep -i "termux-sensor" | grep -v "grep" || log_event "No termux-sensor processes found."

    log_event "${GREEN}✓ Sensor cleanup complete${NC}"
}

# Pre-flight validation - verify gyroscope is accessible
validate_gyro_sensor() {
    log_event "${YELLOW}Validating gyroscope access...${NC}"

    # Try to get TWO samples from gyroscope with 5-second timeout
    # First sample is often empty {}, second has real data
    # Using specific LSM6DSO sensor ID for reliable activation
    if timeout 5 termux-sensor -s "lsm6dso LSM6DSO Gyroscope Non-wakeup" -d 50 -n 2 > "$HOME/.gyro_sensor_test.json" 2>&1; then
        # Check if we got valid JSON output with "values" field
        if grep -q "values" "$HOME/.gyro_sensor_test.json" 2>/dev/null; then
            log_event "${GREEN}✓ Gyroscope responding correctly${NC}"
            rm -f "$HOME/.gyro_sensor_test.json"
            return 0
        fi
    fi

    log_event "${RED}✗ Gyroscope validation failed${NC}"
    # Show what we got for debugging
    if [ -f "$HOME/.gyro_sensor_test.json" ]; then
        log_event "${RED}Output received:${NC}"
        cat "$HOME/.gyro_sensor_test.json" >&2
    fi
    rm -f "$HOME/.gyro_sensor_test.json"
    return 1
}

# Retry mechanism for sensor initialization
initialize_gyro_sensor_with_retry() {
    local max_attempts=3
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        log_event "\n${YELLOW}Gyroscope initialization attempt $attempt/$max_attempts${NC}"

        cleanup_sensors

        if validate_gyro_sensor; then
            return 0
        fi

        if [ $attempt -lt $max_attempts ]; then
            log_event "${YELLOW}Retrying in 3 seconds...${NC}"
            sleep 3
        fi

        attempt=$((attempt + 1))
    done

    log_event "\n${RED}ERROR: Failed to initialize gyroscope after $max_attempts attempts${NC}"
    log_event "${RED}Troubleshooting steps:${NC}"
    log_event "  1. Check sensor permissions: termux-sensor -l"
    log_event "  2. Restart Termux app completely"
    log_event "  3. Verify phone's gyroscope works in other apps"
    log_event "  4. Check for conflicting apps using gyroscope"
    return 1
}

# Cleanup handler for script exit
cleanup_on_exit() {
    local exit_code=$?
    log_event "\n${YELLOW}Data collection finished, performing final cleanup...${NC}"
    cleanup_sensors
    exit $exit_code
}

# Register cleanup handler
trap cleanup_on_exit EXIT SIGINT SIGTERM

# Main execution
DURATION=$1
OUTPUT_FILE="gyro_data.json" # Store in current working directory

if [ -z "$DURATION" ]; then
    log_event "${RED}Usage: $0 <duration_in_seconds>${NC}"
    exit 1
fi

log_event "=============================================================================="
log_event "Raw Gyroscope Data Collection"
log_event "Duration: ${DURATION}s"
log_event "Output file: ${OUTPUT_FILE}"
log_event "=============================================================================="

# Step 1: Initialize gyroscope with retry
if ! initialize_gyro_sensor_with_retry; then
    exit 1
fi

# Step 2: Brief pause to ensure sensor resources are stable
log_event "\n${GREEN}✓ Gyroscope ready, starting data collection in 2 seconds...${NC}"
sleep 2

# Step 3: Collect data
log_event "${GREEN}Collecting raw gyroscope data for ${DURATION} seconds...${NC}"
# DO NOT use stdbuf - breaks Termux:API socket IPC
# Use bufsize=1 (line buffering) via Python subprocess instead
# Use the specific sensor name
termux-sensor -s "lsm6dso LSM6DSO Gyroscope Non-wakeup" -d 50 > "$OUTPUT_FILE" &
SENSOR_PID=$!

# Wait for the specified duration with a progress indicator
log_event "Collection running (PID: $SENSOR_PID)..."
sleep "$DURATION"
log_event "Duration completed."

# Kill the sensor process
log_event "Stopping sensor process (PID: $SENSOR_PID)..."
if kill "$SENSOR_PID" 2>/dev/null; then
    # Wait up to 2 seconds for graceful termination
    for i in {1..20}; do
        if ! kill -0 "$SENSOR_PID" 2>/dev/null; then
            break
        fi
        sleep 0.1
    done

    # Force kill if still running
    if kill -0 "$SENSOR_PID" 2>/dev/null; then
        log_event "Forcing termination with SIGKILL..."
        kill -9 "$SENSOR_PID" 2>/dev/null || true
    fi
fi
log_event "Sensor process stopped."

log_event "${GREEN}✓ Data collection complete. Output saved to ${OUTPUT_FILE}${NC}"
