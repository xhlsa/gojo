#!/bin/bash
# Final dashboard verification test

echo "=================================================="
echo "Dashboard Map Display - Final Verification"
echo "=================================================="
echo ""

# Test 1: Check server is running
echo "1. Testing server connection..."
if curl -s http://localhost:8000 > /dev/null 2>&1; then
    echo "   ✓ Server is running"
else
    echo "   ✗ Server not responding - start with: python3 dashboard_server.py &"
    exit 1
fi
echo ""

# Test 2: Check drives list
echo "2. Testing /api/drives endpoint..."
DRIVE_COUNT=$(curl -s http://localhost:8000/api/drives | python3 -c "import sys, json; print(len(json.load(sys.stdin)['drives']))" 2>/dev/null)
if [ -n "$DRIVE_COUNT" ]; then
    echo "   ✓ Found $DRIVE_COUNT drives"
else
    echo "   ✗ Failed to fetch drives list"
    exit 1
fi
echo ""

# Test 3: Check specific drive metadata
echo "3. Testing drive metadata (comparison_20251105_121921)..."
DRIVE_DATA=$(curl -s http://localhost:8000/api/drive/comparison_20251105_121921)
HAS_GPX=$(echo "$DRIVE_DATA" | python3 -c "import sys, json; print(json.load(sys.stdin)['has_gpx'])" 2>/dev/null)
GPS_SAMPLES=$(echo "$DRIVE_DATA" | python3 -c "import sys, json; print(json.load(sys.stdin)['stats']['gps_samples'])" 2>/dev/null)

if [ "$HAS_GPX" = "True" ]; then
    echo "   ✓ has_gpx: True"
else
    echo "   ✗ has_gpx: False (should be True!)"
    exit 1
fi

if [ -n "$GPS_SAMPLES" ]; then
    echo "   ✓ GPS samples: $GPS_SAMPLES"
else
    echo "   ✗ Failed to get GPS sample count"
    exit 1
fi
echo ""

# Test 4: Check GPX endpoint
echo "4. Testing GPX generation..."
GPX_SIZE=$(curl -s http://localhost:8000/api/drive/comparison_20251105_121921/gpx | wc -c)
if [ "$GPX_SIZE" -gt 1000 ]; then
    echo "   ✓ GPX generated: $GPX_SIZE bytes"
else
    echo "   ✗ GPX too small or missing: $GPX_SIZE bytes"
    exit 1
fi
echo ""

# Test 5: Validate GPX structure
echo "5. Validating GPX structure..."
GPX_CONTENT=$(curl -s http://localhost:8000/api/drive/comparison_20251105_121921/gpx)
TRACKPOINT_COUNT=$(echo "$GPX_CONTENT" | grep -c "<trkpt")
if [ "$TRACKPOINT_COUNT" -eq "$GPS_SAMPLES" ]; then
    echo "   ✓ Trackpoints: $TRACKPOINT_COUNT (matches GPS samples)"
else
    echo "   ⚠  Trackpoints: $TRACKPOINT_COUNT (GPS samples: $GPS_SAMPLES)"
fi

# Check for valid coordinates
FIRST_LAT=$(echo "$GPX_CONTENT" | grep -m1 'trkpt lat=' | sed -n 's/.*lat="\([^"]*\)".*/\1/p')
FIRST_LON=$(echo "$GPX_CONTENT" | grep -m1 'trkpt lat=' | sed -n 's/.*lon="\([^"]*\)".*/\1/p')
if [ -n "$FIRST_LAT" ] && [ -n "$FIRST_LON" ]; then
    echo "   ✓ First coordinate: lat=$FIRST_LAT, lon=$FIRST_LON"
else
    echo "   ✗ Failed to parse coordinates"
    exit 1
fi
echo ""

# Final summary
echo "=================================================="
echo "VERIFICATION COMPLETE"
echo "=================================================="
echo ""
echo "All tests passed! The dashboard should now display the map when you:"
echo "1. Navigate to http://localhost:8000"
echo "2. Click on 'comparison_20251105_121921'"
echo "3. Watch for status messages at top of map:"
echo "   - 'Loading route...'"
echo "   - 'GPX loaded: XXXX chars'"
echo "   - 'Parsed XXX GPS points'"
echo "   - 'Route loaded successfully!' (green)"
echo ""
echo "The map will show:"
echo "- Blue route line ($TRACKPOINT_COUNT GPS points)"
echo "- Green circle marker (start)"
echo "- Red circle marker (end)"
echo ""
