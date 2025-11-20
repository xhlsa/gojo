#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

FIRST_RUN_LOGGED=false

log() {
    local msg="$1"
    if [ "${FIRST_RUN_LOGGED}" = false ]; then
        printf '====\n' >> "$LOG_FILE"
        FIRST_RUN_LOGGED=true
    fi
    printf '[%s] %s\n' "$(date -u)" "$msg" >> "$LOG_FILE"
}

die() {
    log "ERROR: $1"
    exit 1
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ARGS_FILE="$SCRIPT_DIR/ekf_job_args.txt"
LOG_FILE="$SCRIPT_DIR/ekf_job.log"

# ============================================================================
# SENSOR CLEANUP (ensures stale processes don't block access)
# ============================================================================
cleanup_sensors() {
    log "Cleaning up stale sensor processes..."
    pkill -9 -f "termux-sensor.*ACCELEROMETER" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*Accelerometer" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*GYROSCOPE" 2>/dev/null || true
    pkill -9 -f "termux-sensor.*Gyroscope" 2>/dev/null || true
    pkill -9 -f "termux-api Sensor" 2>/dev/null || true
    pkill -9 -f "stdbuf.*termux-sensor" 2>/dev/null || true
    pkill -9 -f "test_ekf_vs_complementary.py" 2>/dev/null || true
    sleep 5  # Android needs time to release sensor HAL
    log "Sensor cleanup complete"
}

cleanup_gps() {
    log "Cleaning up stale GPS processes..."
    pkill -9 -f "termux-location" 2>/dev/null || true
    pkill -9 -f "termux-api Location" 2>/dev/null || true
    sleep 5  # Android needs time to release GPS socket resources
    log "GPS cleanup complete"
}

cleanup_on_exit() {
    local exit_code=$?
    log "Performing final cleanup..."
    cleanup_sensors
    cleanup_gps
    exit $exit_code
}

trap cleanup_on_exit EXIT SIGINT SIGTERM

log "Job started (cwd=$REPO_DIR, args file=$ARGS_FILE)"

# Pre-run cleanup
cleanup_sensors
cleanup_gps

RUNNER="$REPO_DIR/motion_tracker_rs.sh"

if [ ! -x "$RUNNER" ]; then
    die "motion_tracker_rs.sh not executable"
fi

if [ -s "$ARGS_FILE" ]; then
    mapfile -t JOB_ARGS < "$ARGS_FILE"
else
    JOB_ARGS=(10)
fi

cd "$REPO_DIR"
log "Launching motion_tracker_rs.sh ${JOB_ARGS[*]} via $PREFIX/bin/bash"
if "$PREFIX/bin/bash" "$RUNNER" "${JOB_ARGS[@]}" >> "$LOG_FILE" 2>&1; then
    log "motion_tracker_rs.sh completed successfully"
else
    status=$?
    die "motion_tracker_rs.sh exited with code $status"
fi
