# EKF Filter Drive Test - Quick Start Guide

## Tomorrow's Test Plan

### Before You Leave (Setup)
```bash
cd ~/gojo

# Make sure scripts are executable
chmod +x motion_tracker_v2/test_ekf_vs_complementary.py
chmod +x motion_tracker_v2/analyze_comparison.py
```

### During the Drive (Real-Time Monitoring)

**Option 1: Simple 5-minute test**
```bash
cd ~/gojo
python motion_tracker_v2/test_ekf_vs_complementary.py 5
```

**Option 2: Longer test with more data**
```bash
python motion_tracker_v2/test_ekf_vs_complementary.py 15  # 15 minutes
```

**What You'll See:**
```
[00:01] FILTER COMPARISON
────────────────────────────────────────────────────────────
METRIC                    | EKF              | COMPLEMENTARY    | DIFF
────────────────────────────────────────────────────────────
Velocity (m/s)            |    5.123 m/s     |    5.087 m/s     |  0.036 m/s
Distance (m)              |  102.45 m        |  101.23 m        |  1.20%
Accel Magnitude (m/s²)    |    0.324 m/s²    |    0.318 m/s²    |  0.006 m/s²
Status                    | MOVING           | MOVING           |
────────────────────────────────────────────────────────────
GPS fixes: 1 | Accel samples: 42
```

**What It Means:**
- **Velocity**: How fast you're going (EKF should be smoother than Complementary)
- **Distance**: Total distance traveled (should match GPS closely)
- **Accel Magnitude**: Real-time acceleration (useful during acceleration/braking)
- **Status**: MOVING or STATIONARY (important for traffic light detection)

### After You Return (Analysis)

**Find the comparison file:**
```bash
ls -lh motion_tracker_v2/comparison_*.json | tail -1
```

**Run analysis:**
```bash
python motion_tracker_v2/analyze_comparison.py motion_tracker_v2/comparison_2025-10-29_14-30-00.json
```

**What You'll See:**

1. **Distance Accuracy Section**
   ```
   GPS Truth Distance: 5234.82 m

   EKF Filter:
     Reported Distance: 5238.45 m
     Error: 3.63 m (0.07%)

   Complementary Filter:
     Reported Distance: 5220.12 m
     Error: 14.70 m (0.28%)

   ✓ EKF is 75.4% MORE ACCURATE than Complementary
   ```
   **Goal: < 5% error**

2. **Velocity Smoothness Section**
   ```
   EKF Filter:
     Mean Velocity: 12.456 m/s
     Std Dev: 0.287 m/s (lower = smoother)

   Complementary Filter:
     Mean Velocity: 12.312 m/s
     Std Dev: 1.234 m/s

   ✓ EKF is 76.8% SMOOTHER (lower variance)
   ```
   **Goal: Lower std dev = better (less jitter)**

3. **Overall Quality Score**
   ```
   EKF Filter Score:          87.3/100
   Complementary Filter Score: 72.1/100

   ✓ EKF WINS by 15.2 points
   ```
   **Interpretation:**
   - **85-100**: Production-ready ✓
   - **75-85**: Promising, needs minor tuning
   - **<75**: Needs improvement

## Key Metrics to Understand

| Metric | Good Value | What It Measures |
|--------|-----------|-----------------|
| Distance Error % | < 5% | Accuracy vs GPS ground truth |
| Velocity Std Dev | < 0.5 m/s | Smoothness (lower is better) |
| EKF Score | > 85/100 | Overall filter quality |

## What Makes a Good Test

✅ **Good test conditions:**
- Normal driving (not aggressive)
- Some time at traffic lights/stops
- Mix of acceleration and cruising
- GPS signal stays strong (not driving in tunnels)

⚠️ **Avoid:**
- Heavy traffic with constant stop-and-go (confuses GPS)
- Tunnels/underpasses (no GPS signal)
- Very straight roads only (prefer varied routes)

## Example: Interpreting Results

### Scenario A: EKF Wins
```
EKF Error: 2.3%
Complementary Error: 8.5%
EKF Smoother: Yes (0.2 vs 0.8 std dev)
Score: 92/100

→ SUCCESS! EKF is significantly better
→ Ready to make default filter
```

### Scenario B: Tie/Mixed
```
EKF Error: 4.2%
Complementary Error: 4.8%
EKF Smoother: Yes (0.3 vs 0.9 std dev)
Score: 78/100

→ EKF has advantages in smoothness
→ Consider filter tuning or more testing
→ May need gyroscope for better accuracy
```

### Scenario C: Complementary Wins
```
EKF Error: 6.5%
Complementary Error: 2.1%
EKF Smoother: No
Score: 65/100

→ EKF underperforming
→ Check GPS accuracy/signal quality
→ Review accelerometer calibration
→ Possible sensor issues
```

## Troubleshooting

**"Failed to get GPS lock"**
- Wait in open area for 30+ seconds
- Try test again in different location

**"Accel daemon failed to start"**
- Check if `termux-sensor` is installed
- Try: `termux-sensor -s ACCELEROMETER` manually

**"Very high error rates"**
- GPS accuracy might be poor
- Check with `termux-location` first
- Try short test to verify sensor startup

## Files Generated

After test, you'll have:
```
motion_tracker_v2/
├── comparison_2025-10-29_14-30-45.json    ← Raw data (keep for analysis)
└── analysis_results.txt                   ← Analysis output
```

**Keep the JSON file!** You can re-analyze later with different parameters.

## Next Steps After Test

1. **If EKF wins (score > 85):**
   - ✓ Make EKF the default filter
   - ✓ Test with `--enable-gyro` flag

2. **If results are mixed (score 75-85):**
   - Test with gyroscope enabled
   - Try another route for comparison
   - Review sensor calibration

3. **If Complementary wins:**
   - Review GPS/sensor quality
   - Check for multipath issues (reflections)
   - Consider different tuning parameters

---

**Ready to test? Good luck! 🚗**
