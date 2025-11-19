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

if [ ! -x "$REPO_DIR/test_ekf.sh" ]; then
    die "test_ekf.sh not executable"
fi

if [ -s "$ARGS_FILE" ]; then
    mapfile -t JOB_ARGS < "$ARGS_FILE"
else
    JOB_ARGS=(10)
fi

cd "$REPO_DIR"
log "Launching test_ekf.sh ${JOB_ARGS[*]} via $PREFIX/bin/bash"
if "$PREFIX/bin/bash" "$REPO_DIR/test_ekf.sh" "${JOB_ARGS[@]}" >> "$LOG_FILE" 2>&1; then
    log "test_ekf.sh completed successfully"
else
    status=$?
    die "test_ekf.sh exited with code $status"
fi
