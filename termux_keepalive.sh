#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

usage() {
    cat <<USAGE
Usage: $0 [options]
  --cancel          Cancel keep-alive state
  --stay-awake      Enable Android "stay awake while charging"
  --set-awake       Termux power stay-on true
USAGE
}

if [ ${1:-} == "--cancel" ]; then
    termux-wake-unlock
    termux-notification-remove tracker-lock || true
    termux-toast "Keep-alive cancelled"
    exit 0
fi

termux-wake-lock
termux-notification --id tracker-lock --content "Motion tracker running" --priority high --ongoing
termux-toast "Keep-alive enabled"
