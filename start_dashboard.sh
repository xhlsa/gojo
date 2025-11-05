#!/bin/bash
# Motion Tracker Dashboard Server Launcher

cd "$(dirname "$0")" || exit 1

echo "================================================"
echo "Motion Tracker Dashboard Server"
echo "================================================"
echo ""
echo "Starting FastAPI server..."
echo "Access dashboard at: http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop the server"
echo "================================================"
echo ""

python3 dashboard_server.py
