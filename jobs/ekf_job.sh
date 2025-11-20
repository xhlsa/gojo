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

log "Job started (cwd=$REPO_DIR, args file=$ARGS_FILE)"

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
