#!/bin/bash
DURATION=$1
OUTPUT_FILE="gyro_data.json" # Store in current working directory

echo "Starting collection script..."
echo "Duration: $DURATION"
echo "Output file: $OUTPUT_FILE"

# Start termux-sensor and redirect output to a file.
# The `timeout` command will automatically kill the process after the specified duration.
echo "Running timeout command..."
timeout $DURATION termux-sensor -s gyroscope -d 50 | tee $OUTPUT_FILE
EXIT_CODE=$?
echo "Timeout command finished with exit code: $EXIT_CODE"

echo "Collection script finished."
