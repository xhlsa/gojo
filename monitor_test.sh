#!/bin/bash
LOGFILE="/data/data/com.termux/files/home/gojo/crash_logs/test_ekf_2025-11-08_12-36-32.log"

echo "Monitoring test... (checking every 60 seconds)"
echo "Will notify when [DEBUG] exit message appears"

while true; do
  if grep -q "\[DEBUG\] Loop exited" "$LOGFILE" 2>/dev/null; then
    echo ""
    echo "=========================================="
    echo "✓ TEST COMPLETED!"
    echo "=========================================="
    tail -5 "$LOGFILE" | grep "\[DEBUG\]"
    break
  fi
  
  # Show current progress (last timestamp)
  LATEST=$(tail -1 "$LOGFILE" 2>/dev/null | grep -o "\[.*\]" | tail -1)
  echo "[$(date +%H:%M:%S)] Test still running... $LATEST"
  
  sleep 60
done
