#!/data/data/com.termux/files/usr/bin/bash
# Entry point for production drives.
# ALWAYS runs directly with wakelock (JobScheduler is too latent for interactive start).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[drive.sh] Acquiring wakelock and starting tracker..." >&2
termux-wake-lock
trap 'termux-wake-unlock' EXIT

exec "$SCRIPT_DIR/motion_tracker_rs.sh" "$@"
