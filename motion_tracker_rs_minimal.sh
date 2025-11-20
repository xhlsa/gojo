#!/bin/bash
# Minimal version - just run the binary, no cleanup
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/motion_tracker_sessions"
mkdir -p "$OUTPUT_DIR"

cd "$SCRIPT_DIR/motion_tracker_rs"

# Convert legacy minute argument to seconds
converted_args=()
if [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; then
    minutes="$1"
    shift
    if [ "$minutes" -eq 0 ]; then
        converted_args+=(0)
    else
        converted_args+=($((minutes * 60)))
    fi
fi

converted_args+=("$@")

# Run the binary directly with output dir
./target/release/motion_tracker --output-dir "$OUTPUT_DIR" "${converted_args[@]}"
