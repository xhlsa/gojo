#!/data/data/com.termux/files/usr/bin/bash
# Entry point for production drives. Prefers the Termux JobScheduler path so Android
# keeps sensors alive when the app is backgrounded, but falls back to a simple
# wakelock wrapper if Termux:API isn't available.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEDULER="$SCRIPT_DIR/schedule_test_ekf.sh"

if command -v termux-job-scheduler >/dev/null 2>&1; then
    if [ ! -x "$SCHEDULER" ]; then
        echo "[drive.sh] schedule_test_ekf.sh missing or not executable" >&2
        exit 1
    fi
    echo "[drive.sh] Using Termux JobScheduler foreground job (see jobs/ekf_job.log for output)"
    exec "$SCHEDULER" "$@"
fi

echo "[drive.sh] termux-job-scheduler not available; falling back to wakelock wrapper" >&2
termux-wake-lock
trap 'termux-wake-unlock' EXIT

exec "$SCRIPT_DIR/motion_tracker_rs.sh" "$@"
