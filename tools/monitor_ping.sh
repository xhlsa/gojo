#!/bin/bash
LOG_FILE="ping_monitor.log"
echo "=== Ping Tracker Monitor Started at $(date) ===" > "$LOG_FILE"

for i in {1..8}; do
    sleep 900  # 15 minutes
    echo "" >> "$LOG_FILE"
    echo "=== Check-in $i at $(date) ===" >> "$LOG_FILE"
    tmux capture-pane -t ping-tracker-long -p 2>/dev/null | tail -25 >> "$LOG_FILE" || echo "Session ended" >> "$LOG_FILE"
done

echo "" >> "$LOG_FILE"
echo "=== Monitoring complete at $(date) ===" >> "$LOG_FILE"
