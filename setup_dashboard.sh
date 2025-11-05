#!/bin/bash
# Setup script for Motion Tracker Dashboard

echo "================================================"
echo "Motion Tracker Dashboard - Setup"
echo "================================================"
echo ""

# Check if FastAPI is installed
if ! python3 -c "import fastapi" 2>/dev/null; then
    echo "Installing required dependencies..."
    pip install fastapi uvicorn -q
    if [ $? -eq 0 ]; then
        echo "✓ Dependencies installed successfully"
    else
        echo "✗ Failed to install dependencies"
        echo "Try: pip install fastapi uvicorn"
        exit 1
    fi
else
    echo "✓ FastAPI already installed"
fi

echo ""
echo "================================================"
echo "Setup complete! Starting dashboard..."
echo "================================================"
echo ""

python3 dashboard_server.py
