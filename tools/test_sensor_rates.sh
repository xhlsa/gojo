#!/bin/bash
# Test various sensor polling approaches to measure actual data rates

echo "=== SENSOR POLLING RATE COMPARISON ==="
echo ""

# Test 1: termux-sensor with different delay values
echo "Test 1: termux-sensor with various delay parameters (5s, count data only)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

for delay in 0 5 10 20 50 100; do
    echo ""
    echo "Delay: ${delay}ms"

    # Collect data for 5 seconds and count non-empty objects
    output=$(timeout 5 stdbuf -oL termux-sensor -s ACCELEROMETER -d "$delay" 2>&1)

    # Count valid JSON objects (those with "values" key)
    count=$(echo "$output" | grep -c '"values"')
    echo "  Valid samples in 5s: $count"
    echo "  Rate: $(echo "scale=1; $count / 5" | bc) Hz"
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Test 2: Direct termux-api calls (if possible)
echo "Test 2: Timing characteristics of single API calls"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Time 10 sequential API calls
start=$(date +%s%N)
for i in {1..10}; do
    /data/data/com.termux/files/usr/libexec/termux-api Sensor -a sensors --es sensors "ACCELEROMETER" --ei limit 1 2>/dev/null > /dev/null
done
end=$(date +%s%N)

elapsed_ms=$(echo "scale=1; ($end - $start) / 1000000" | bc)
avg_ms=$(echo "scale=1; $elapsed_ms / 10" | bc)
max_rate=$(echo "scale=1; 1000 / $avg_ms" | bc)

echo "10 sequential API calls took: ${elapsed_ms}ms"
echo "Average per call: ${avg_ms}ms"
echo "Maximum theoretical rate: ${max_rate} Hz"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Summary:"
echo "  - termux-sensor is a wrapper around termux-api Sensor calls"
echo "  - Minimum viable delay appears to be ~10ms based on hardware"
echo "  - Hardware limits maximum rate regardless of software delay setting"
echo "  - Direct API calls likely have similar overhead to termux-sensor"
