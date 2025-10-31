# EKF vs Complementary Baseline Validation - Quick Start Guide

**Status:** ✅ Ready for Real-World Testing
**Date:** Oct 31, 2025
**Goal:** Collect evidence-based comparison of 13D Gyro-EKF vs Complementary Filter across real drives

---

## What's Ready

✅ **test_ekf_vs_complementary.py** - Now production-grade with:
- Gzip compression (20-30% file size)
- Clear-after-save mechanism (unlimited data collection)
- Atomic file operations (corruption-safe)
- Proper session organization (motion_tracker_sessions/)

✅ **analyze_ekf_baseline.py** - Automated analysis that:
- Loads all comparison JSON/gzipped files
- Calculates GPS ground truth (haversine formula)
- Computes error % for both filters
- Generates comparison table + statistics + verdict

✅ **COMPARISON_BASELINE_TRACKER.md** - Tracking framework with:
- Metrics template per drive
- Drive log template
- Analysis questions to answer over time
- Decision criteria for EKF vs Complementary

---

## The Workflow

### Step 1: Collect Real-World Data
```bash
./test_ekf.sh 30 --gyro          # 30-minute drive test (EKF vs Complementary)
```

**Output:**
- Saves: `motion_tracker_sessions/comparison_TIMESTAMP.json.gz`
- Size: ~2-3 MB (gzipped)
- Metrics: Distance, incidents, memory, GPS fixes, filter health

**What happens:**
1. Starts 2-minute auto-save cycle
2. Each save: writes to disk, clears in-memory deques
3. Enables unlimited collection (memory stays at 92 MB)
4. At end: final comparison file saved

### Step 2: Analyze Results
After 1+ drives:
```bash
python3 motion_tracker_v2/analyze_ekf_baseline.py
```

**Output:**
```
============================================================
EKF vs COMPLEMENTARY FILTER - PERFORMANCE COMPARISON
============================================================
Date/Time            GPS Dist    EKF Err %    Comp Err %    EKF Adv %
--------
20251031_120000      5234.1 m       2.3%         3.8%        +1.5%
20251101_140000      8902.4 m       1.9%         3.1%        +1.2%
...

============================================================
SUMMARY STATISTICS
============================================================
Tests analyzed:        2

EKF Error:
  Mean:                 2.10%
  Std Dev:              0.28%
  Range:                1.82% - 2.38%

Complementary Error:
  Mean:                 3.45%
  Std Dev:              0.49%
  Range:                3.07% - 3.83%

EKF Advantage:
  Mean:                +1.35% better
  Std Dev:              0.21%
  Range:               +1.14% - +1.56%

Consistency:           CONSISTENT (±2-3%)

...

============================================================
WIN RECORD
============================================================
EKF wins:              2 / 2
Complementary wins:    0 / 2
Ties:                  0 / 2

EKF Win Rate:          100.0%
Verdict:               ✅ EKF CLEARLY SUPERIOR - Justify increased complexity
```

---

## Key Metrics Explained

| Metric | What It Means | Normal Range |
|--------|---------------|--------------|
| **GPS Distance** | Haversine ground truth from GPS points | Varies |
| **EKF Error %** | \|EKF_distance - GPS_distance\| / GPS_distance * 100 | 0-5% |
| **Comp Error %** | Same for Complementary filter | 0-10% |
| **EKF Advantage %** | Comp_error - EKF_error (positive = EKF better) | +0.5 to +5% |
| **Consistency** | Range of EKF advantage across drives | <2% = VERY CONSISTENT |

---

## Decision Criteria

**After collecting 5-10 drives:**

✅ **Choose EKF if:**
- Consistently outperforms by 2-3%+
- Advantage holds across different conditions
- Win record 80%+ in favor of EKF
- Memory/CPU overhead acceptable (it is - 92 MB)

⚠️ **Reconsider if:**
- Advantage <1% across the board
- Complementary wins in common scenarios
- Inconsistent performance
- Similar win/loss record

---

## Test Checklist

Before each drive:
- [ ] Check device is safely mounted
- [ ] Ensure 10%+ battery remaining
- [ ] Verify motion_tracker_sessions/ directory exists
- [ ] Note the route/conditions (highway, city, mixed, weather)

After each drive:
- [ ] Wait for final output "✓ Test completed"
- [ ] Check file exists: `ls -lh motion_tracker_sessions/comparison_*.json.gz | tail -1`
- [ ] File should be 2-5 MB (smaller = shorter drive)
- [ ] If 0 bytes = error occurred, check logs

---

## File Organization

```
gojo/
├── motion_tracker_v2/
│   ├── test_ekf_vs_complementary.py      (Test framework - IMPROVED)
│   └── analyze_ekf_baseline.py           (Analysis tool - NEW)
├── motion_tracker_sessions/
│   ├── comparison_20251031_120000.json.gz   (Test 1)
│   ├── comparison_20251101_140000.json.gz   (Test 2)
│   └── ... (more as you collect data)
├── COMPARISON_BASELINE_TRACKER.md        (Tracking framework)
└── BASELINE_VALIDATION_QUICKSTART.md     (This file)
```

---

## Commands Reference

```bash
# Run validation test (30 minutes)
./test_ekf.sh 30 --gyro

# Analyze all collected data
python3 motion_tracker_v2/analyze_ekf_baseline.py

# Analyze specific file
python3 motion_tracker_v2/analyze_ekf_baseline.py motion_tracker_sessions/comparison_20251031_120000.json.gz

# Check collected files
ls -lh motion_tracker_sessions/comparison_*.json.gz | tail -5

# Monitor real-time during test
tail -f motion_tracker_sessions/motion_track_v2_*.json
```

---

## Expected Outcomes

### Short Term (After 2-3 drives)
- First pass verdict on whether EKF is worth the complexity
- Initial pattern identification (highway vs city?)
- Confidence in test methodology

### Medium Term (5-10 drives)
- Statistical significance (enough data for final decision)
- Consistency pattern across conditions
- Ready to commit to one filter as default

### Long Term (20+ drives)
- Publication-quality dataset
- Detailed documentation of when each filter excels
- Open-source validation proof

---

## Troubleshooting

**Q: Test fails with "No accelerometer data"**
A: Run: `pkill -9 termux-sensor && sleep 3 && ./test_ekf.sh 5 --gyro`

**Q: File size is 0 bytes**
A: Test crashed. Check: `tail -100 test_ekf.sh` for error messages

**Q: analyze_ekf_baseline.py says "No successful analyses found"**
A: Make sure comparison_*.json.gz files exist in motion_tracker_sessions/

**Q: Results show EKF win rate 100%**
A: This is good news! But run more drives in different conditions to ensure consistency

---

## Next Steps

1. **Today:** Run 1-2 validation drives (15-30 minutes each)
2. **This Week:** Collect 5+ drives across different conditions
3. **Decision Point:** After 5-10 drives, run analysis and decide whether to make EKF the default

---

**Ready to validate. Start collecting real-world data now.**

```bash
./test_ekf.sh 30 --gyro
```

