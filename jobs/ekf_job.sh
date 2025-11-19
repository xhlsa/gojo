#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

die() {
    echo "[ekf_job] $1" >&2
    exit 1
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ARGS_FILE="$SCRIPT_DIR/ekf_job_args.txt"
LOG_FILE="$SCRIPT_DIR/ekf_job.log"

echo "[$(date -u)] Job started" >> "$LOG_FILE"

if [ ! -x "$REPO_DIR/test_ekf.sh" ]; then
    die "test_ekf.sh not executable"
fi

if [ -s "$ARGS_FILE" ]; then
    mapfile -t JOB_ARGS < "$ARGS_FILE"
else
    JOB_ARGS=(10)
fi

cd "$REPO_DIR"
exec ./test_ekf.sh "${JOB_ARGS[@]}"
