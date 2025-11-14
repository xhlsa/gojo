#!/bin/bash
# Accelerometer Load Test
# Test filter architecture with different sampling rates

echo "=================================================================="
echo "ACCELEROMETER LOAD TEST"
echo "=================================================================="
echo ""
echo "Testing filter architecture with progressively higher sampling rates"
echo "Each test runs for 1 minute"
echo ""

# Test rates: delay_ms -> expected Hz
# delay_ms = 50 -> 20 Hz (baseline)
# delay_ms = 20 -> 50 Hz (2.5x)
# delay_ms = 10 -> 100 Hz (5x)
# delay_ms = 5  -> 200 Hz (10x)

tests=(
    "50:20Hz_baseline"
    "20:50Hz_moderate"
    "10:100Hz_high"
)

results_dir="$HOME/gojo/logs/load_tests"
mkdir -p "$results_dir"

echo "Will run ${#tests[@]} tests:"
for test in "${tests[@]}"; do
    delay=$(echo $test | cut -d: -f1)
    label=$(echo $test | cut -d: -f2)
    expected_hz=$((1000 / delay))
    echo "  - Delay ${delay}ms (~${expected_hz} Hz) - $label"
done
echo ""

read -p "Press Enter to start..."
echo ""

for test in "${tests[@]}"; do
    delay=$(echo $test | cut -d: -f1)
    label=$(echo $test | cut -d: -f2)

    echo "=================================================================="
    echo "TEST: $label (delay=${delay}ms)"
    echo "=================================================================="

    log_file="$results_dir/load_test_${label}.log"

    # Temporarily modify delay in test file
    sed -i.bak "s/delay_ms=50/delay_ms=$delay/g" ~/gojo/motion_tracker_v2/test_ekf_vs_complementary.py

    # Run test
    cd ~/gojo && timeout 90 ./test_ekf.sh 1 --gyro > "$log_file" 2>&1
    exit_code=$?

    # Restore original delay
    mv ~/gojo/motion_tracker_v2/test_ekf_vs_complementary.py.bak ~/gojo/motion_tracker_v2/test_ekf_vs_complementary.py

    if [ $exit_code -eq 0 ]; then
        # Parse results
        accel_count=$(grep -o '\[ACCEL_LOOP\] Processed sample #[0-9]*' "$log_file" | tail -1 | grep -o '[0-9]*$')
        queue_warns=$(grep -c 'Queue.*backing up' "$log_file")
        memory=$(grep 'Peak memory' "$log_file" | grep -o '[0-9.]*' | head -1)

        if [ -n "$accel_count" ]; then
            actual_hz=$(echo "scale=1; $accel_count / 60" | bc)
            echo "✓ Test completed"
            echo "  Samples collected: $accel_count"
            echo "  Actual rate: ${actual_hz} Hz"
            echo "  Queue warnings: $queue_warns"
            echo "  Memory: ${memory} MB"

            if [ "$queue_warns" -gt 0 ]; then
                echo "  ⚠️  Queue backup detected - system under stress"
                echo "  Stopping progressive test"
                break
            fi
        else
            echo "❌ Could not parse results"
        fi
    else
        echo "❌ Test failed (exit code: $exit_code)"
        break
    fi

    echo ""
    sleep 3
done

echo "=================================================================="
echo "LOAD TEST SUMMARY"
echo "=================================================================="
echo ""
echo "Results saved to: $results_dir"
echo ""
ls -lh "$results_dir"
echo ""
echo "To view a specific test:"
echo "  cat $results_dir/load_test_<label>.log"
echo ""
