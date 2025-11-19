#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

JOB_ID=4201
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JOB_DIR="$SCRIPT_DIR/jobs"
JOB_SCRIPT="$JOB_DIR/ekf_job.sh"
ARGS_FILE="$JOB_DIR/ekf_job_args.txt"

usage() {
    cat <<USAGE
Usage: $0 [--cancel] [test-args...]
  --cancel       Cancel the scheduled EKF job
  test-args      Arguments forwarded to test_ekf.sh (default: 10)
USAGE
}

if [ $# -gt 0 ] && [ "$1" = "--help" ]; then
    usage
    exit 0
fi

if [ $# -gt 0 ] && [ "$1" = "--cancel" ]; then
    if termux-job-scheduler --cancel $JOB_ID >/dev/null 2>&1; then
        echo "Cancelled job $JOB_ID"
    else
        echo "No existing job $JOB_ID" >&2
    fi
    exit 0
fi

if ! command -v termux-job-scheduler >/dev/null 2>&1; then
    echo "termux-job-scheduler not available. Install Termux:API." >&2
    exit 1
fi

mkdir -p "$JOB_DIR"

if [ $# -eq 0 ]; then
    set -- 10
fi

: > "$ARGS_FILE"
for arg in "$@"; do
    printf '%s\n' "$arg" >> "$ARGS_FILE"
fi

termux-job-scheduler --cancel $JOB_ID >/dev/null 2>&1 || true

termux-job-scheduler \
  --job-id $JOB_ID \
  --script "$JOB_SCRIPT" \
  --persisted true \
  --network any \
  --requires-charging false \
  --requires-battery-not-low false \
  --deadline-ms 1000 >/dev/null

echo "Scheduled EKF job ($JOB_ID) with arguments: $*"
echo "Job will run test_ekf.sh in foreground service via Termux scheduler"
