#!/bin/bash
# Simple bash wrapper for termux accelerometer sensor reading
# Usage: accel_reader.sh [--continuous SECONDS] [--interval SECONDS] [--raw]

CONTINUOUS=0
INTERVAL=0.5
RAW=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --continuous|-c) CONTINUOUS="$2"; shift 2 ;;
        --interval|-i) INTERVAL="$2"; shift 2 ;;
        --raw|-r) RAW=true; shift ;;
        --help|-h)
            echo "Usage: $0 [--continuous SECS] [--interval SECS] [--raw]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Simple function to read one sample with killing the sensor after
read_one_sample() {
    {
        sleep 1.5
        pkill -f termux-sensor 2>/dev/null || true
    } &
    timeout 3 stdbuf -oL termux-sensor -s ACCELEROMETER 2>/dev/null | head -20
}

# Single read
if (( CONTINUOUS == 0 )); then
    read_one_sample
    exit 0
fi

# Continuous reads
echo "Reading accelerometer for ${CONTINUOUS}s (interval: ${INTERVAL}s)" >&2
start=$(date +%s%N)

while true; do
    now=$(date +%s%N)
    elapsed=$(echo "scale=1; ($now - $start) / 1000000000" | bc)

    # Check if done
    if (( $(echo "$elapsed >= $CONTINUOUS" | bc -l) )); then
        break
    fi

    echo "[${elapsed%.*}s]"
    read_one_sample

    sleep "$INTERVAL"
done
