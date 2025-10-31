#!/bin/bash
# Quick crash viewer
# Shows recent test crashes and their context

CRASH_DIR="crash_logs"

if [ ! -d "$CRASH_DIR" ]; then
    echo "No crash logs found (directory does not exist)"
    exit 0
fi

echo "================================================================================"
echo "RECENT TEST CRASHES"
echo "================================================================================"
echo ""

# Count crashes
crash_count=$(ls -1 "$CRASH_DIR"/test_ekf_*.log 2>/dev/null | wc -l)
echo "Total crash logs: $crash_count"
echo ""

if [ $crash_count -eq 0 ]; then
    echo "No crashes found!"
    exit 0
fi

# Show list of crashes
echo "Recent crashes (newest first):"
echo "================================================================================"
ls -1t "$CRASH_DIR"/test_ekf_*.log 2>/dev/null | head -10 | while read logfile; do
    timestamp=$(basename "$logfile" | sed 's/test_ekf_//;s/.log//')

    # Try to extract exit code
    exit_code=$(grep "Exit code" "$logfile" 2>/dev/null | tail -1 | grep -o '[0-9]*$' || echo "?")
    signal_info=$(grep "Signal:" "$logfile" 2>/dev/null | tail -1 | sed 's/.*Signal: //' || echo "")

    status="CRASHED"
    if grep -q "successfully" "$logfile" 2>/dev/null; then
        status="SUCCESS"
    fi

    printf "%-21s | Exit: %-3s | %s" "$timestamp" "$exit_code" "$status"
    if [ ! -z "$signal_info" ]; then
        printf " | %s" "$signal_info"
    fi
    echo ""
done

echo ""
echo "================================================================================"
echo "MOST RECENT CRASH DETAILS"
echo "================================================================================"
echo ""

# Show most recent crash
most_recent=$(ls -1t "$CRASH_DIR"/test_ekf_*.log 2>/dev/null | head -1)

if [ ! -z "$most_recent" ]; then
    echo "File: $most_recent"
    echo "-------"

    # Extract key info
    echo ""
    grep -E "^(Exit code|Signal|Test:|Timestamp|CRASH/ERROR)" "$most_recent" 2>/dev/null || true

    # Show last 30 lines of output
    echo ""
    echo "Last 30 lines of test output:"
    echo "-------"
    tail -30 "$most_recent"
else
    echo "No crash logs found"
fi
