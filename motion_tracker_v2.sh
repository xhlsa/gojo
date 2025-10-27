#!/bin/bash
# Motion Tracker V2 launcher - runs the tracker with proper signal handling

set -e  # Exit on errors

# Get directory and change to it
SCRIPT_DIR="$(dirname "$0")"
cd "$SCRIPT_DIR" || exit 1

# Launch Python process and capture PID
python motion_tracker_v2/motion_tracker_v2.py "$@" &
TRACKER_PID=$!

# Forward signals to Python process
trap "kill -TERM $TRACKER_PID 2>/dev/null || true; wait $TRACKER_PID 2>/dev/null || true" SIGINT SIGTERM

# Wait for process and return its exit code
wait $TRACKER_PID
exit_code=$?

exit $exit_code
