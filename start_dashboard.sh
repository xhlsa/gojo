#!/bin/bash
# Motion Tracker Dashboard Server Launcher (Rust)

cd "$(dirname "$0")/motion_tracker_rs" || exit 1

echo "================================================"
echo "Motion Tracker Dashboard Server (Rust)"
echo "================================================"
echo ""
echo "Building dashboard binary..."
cargo build --release --bin dashboard

echo ""
echo "Starting server..."
echo "Access dashboard at: http://localhost:8081"
echo ""
echo "Press Ctrl+C to stop the server"
echo "================================================"
echo ""

# Run the binary with the correct data directory (relative to repo root)
./target/release/dashboard --data-dir ../motion_tracker_sessions