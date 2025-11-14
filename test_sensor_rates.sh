#!/bin/bash
# Test actual sensor sampling rates at different delays

echo "Testing LSM6DSO Accelerometer at different delay values"
echo "=========================================================="
echo ""

for delay in 1 5 10 20 50; do
  echo "Testing delay_ms=$delay..."
  count=$(timeout 5 termux-sensor -s "lsm6dso LSM6DSO Accelerometer" -d $delay -n 10000 2>/dev/null | grep -c "lsm6dso")
  if [ $count -gt 0 ]; then
    hz=$(echo "scale=1; $count / 5" | bc)
    theoretical_hz=$((1000 / delay))
    efficiency=$(echo "scale=1; ($hz / $theoretical_hz) * 100" | bc)
    echo "  Samples: $count in 5s"
    echo "  Actual: ${hz} Hz"
    echo "  Theoretical: ${theoretical_hz} Hz"
    echo "  Efficiency: ${efficiency}%"
  else
    echo "  No samples collected"
  fi
  echo ""
  sleep 1
done

echo "=========================================================="
echo "Current setting: delay_ms=50 (20 Hz theoretical)"
echo "Recommendation based on results above"
