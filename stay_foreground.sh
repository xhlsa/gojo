#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
termux-wake-lock
termux-notification --id motion-tracker --title "Motion Tracker" --content "Keep this notification pinned" --ongoing --priority max
termux-toast "Foreground keep-alive enabled"
