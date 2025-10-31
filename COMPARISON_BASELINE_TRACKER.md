# EKF vs Complementary Filter - Baseline Comparison Tracker

**Purpose:** Track performance gap between 13D Gyro-EKF (chosen) vs Complementary Filter (baseline) across real-world drives

**Status:** Active tracking started Oct 31, 2025

---

## Metrics to Track Per Drive

### Distance Accuracy (vs GPS ground truth)
```
GPS Distance:          [from haversine calculation]
EKF Distance:          [from EKF state]
Complementary Distance: [from Complementary filter]

EKF Error %:           |EKF - GPS| / GPS * 100
Comp Error %:          |Comp - GPS| / GPS * 100
EKF Advantage:         Comp Error % - EKF Error %
```

### Incident Detection Comparison
```
Hard Braking Events:
  EKF count:          [number detected]
  Comp count:         [number detected]
  Difference:         [which detected more?]

Swerving Events:
  EKF count:          [number detected]
  Comp count:         [number detected]
  Difference:         [which detected more?]
```

### Quaternion Health (EKF only)
```
Quaternion Norm Mean:  [should be 1.0]
Quaternion Norm Std:   [should be tight]
Status:                [HEALTHY / WARNING]
```

### Filter Behavior
```
EKF Bias Convergence:  [time to converge]
Gyro Residual:         [bias-corrected error]
Filter Stability:      [any numerical issues?]
```

### System Performance
```
Peak Memory:           [MB]
Test Duration:         [minutes]
GPS Fixes Collected:   [count]
Accel Samples:         [count @ rate]
```

---

## Drive Log Template

```
DATE: YYYY-MM-DD
TIME: HH:MM-HH:MM (duration in minutes)
ROUTE: [description - highway/city/mixed]
CONDITIONS: [weather, traffic, road quality]

DISTANCE ACCURACY:
  GPS Ground Truth:    XXX.X m
  EKF:                 XXX.X m  (±X.X%)
  Complementary:       XXX.X m  (±X.X%)
  Winner:              EKF (+X.X% better)

INCIDENT DETECTION:
  Hard Braking:
    EKF:               X events
    Comp:              X events
    Accuracy:          [both match / EKF better / Comp better]

  Swerving:
    EKF:               X events
    Comp:              X events
    Accuracy:          [both match / EKF better / Comp better]

FILTER METRICS:
  EKF Bias Conv:       [converged Y/N, time: XXs]
  Gyro Residual:       X.XXX rad/s
  Quaternion Norm:     1.000XXX
  Memory Peak:         XX.X MB

NOTES:
  [any observations about filter behavior]
  [any incidents where one filter clearly won]
  [any anomalies or edge cases]

FILE: comparison_TIMESTAMP.json
```

---

## Summary Table (Update After Each Drive)

| Date | Route | Duration | EKF Error | Comp Error | Advantage | Incidents Match | Notes |
|------|-------|----------|-----------|------------|-----------|-----------------|-------|
| 10/31 | [first] | XXm | X.X% | X.X% | EKF +X.X% | Y/N | - |

---

## Analysis Questions to Answer Over Time

### Does EKF consistently outperform?
- [ ] Track every drive
- [ ] Calculate average advantage
- [ ] Note any drives where Complementary wins
- [ ] Identify patterns (highway vs city?)

### Is the gap consistent?
- [ ] Is EKF always better by ~X%?
- [ ] Does it vary with conditions?
- [ ] Does it improve/degrade over time?

### Incident detection reliability
- [ ] Do both filters catch the same incidents?
- [ ] Does EKF detect more subtle incidents?
- [ ] Are false positives similar?

### Is bias convergence working?
- [ ] Does EKF bias converge on every drive?
- [ ] Does convergence time vary?
- [ ] Does bias stay stable?

---

## Comparison Analysis Script

```python
import json
import gzip
from pathlib import Path

def load_comparison_file(filepath):
    """Load and parse comparison JSON file"""
    if filepath.endswith('.gz'):
        with gzip.open(filepath, 'rt') as f:
            return json.load(f)
    else:
        with open(filepath) as f:
            return json.load(f)

def analyze_comparison(filepath):
    """Analyze single comparison test"""
    data = load_comparison_file(filepath)

    ekf_dist = data['final_metrics']['ekf']['distance']
    comp_dist = data['final_metrics']['complementary']['distance']
    gps_samples = data['gps_samples']

    # Calculate GPS ground truth (haversine)
    gps_dist = calculate_gps_distance(gps_samples)

    # Calculate errors
    ekf_error = abs(ekf_dist - gps_dist) / max(gps_dist, 0.001) * 100
    comp_error = abs(comp_dist - gps_dist) / max(gps_dist, 0.001) * 100
    advantage = comp_error - ekf_error

    print(f"GPS Distance:      {gps_dist:.1f} m")
    print(f"EKF Distance:      {ekf_dist:.1f} m  ({ekf_error:.1f}% error)")
    print(f"Comp Distance:     {comp_dist:.1f} m  ({comp_error:.1f}% error)")
    print(f"EKF Advantage:     +{advantage:.1f}% better")
    print()
    print(f"Duration:          {data['actual_duration']:.0f} seconds")
    print(f"Peak Memory:       {data['peak_memory_mb']:.1f} MB")
    print(f"GPS Fixes:         {len(gps_samples)}")

    return {
        'gps_distance': gps_dist,
        'ekf_distance': ekf_dist,
        'comp_distance': comp_dist,
        'ekf_error_pct': ekf_error,
        'comp_error_pct': comp_error,
        'ekf_advantage_pct': advantage,
        'duration': data['actual_duration'],
        'peak_memory': data['peak_memory_mb']
    }

def compare_multiple_drives(directory):
    """Compare trends across multiple test files"""
    comparison_files = sorted(Path(directory).glob('comparison_*.json*'))

    results = []
    for f in comparison_files:
        try:
            result = analyze_comparison(str(f))
            result['file'] = f.name
            results.append(result)
        except Exception as e:
            print(f"Error analyzing {f}: {e}")

    # Calculate averages
    if results:
        avg_ekf_error = sum(r['ekf_error_pct'] for r in results) / len(results)
        avg_comp_error = sum(r['comp_error_pct'] for r in results) / len(results)
        avg_advantage = avg_comp_error - avg_ekf_error

        print("\n" + "="*60)
        print("SUMMARY ACROSS ALL DRIVES")
        print("="*60)
        print(f"Drives analyzed:        {len(results)}")
        print(f"Average EKF error:      {avg_ekf_error:.2f}%")
        print(f"Average Comp error:     {avg_comp_error:.2f}%")
        print(f"Average EKF advantage:  +{avg_advantage:.2f}% better")
        print()

        # Consistency check
        advantages = [r['ekf_advantage_pct'] for r in results]
        print(f"EKF advantage range:    {min(advantages):.2f}% to {max(advantages):.2f}%")
        print(f"Consistency:            {'CONSISTENT' if max(advantages) - min(advantages) < 5 else 'VARIABLE'}")

if __name__ == '__main__':
    compare_multiple_drives('/data/data/com.termux/files/home/gojo/motion_tracker_sessions')
```

---

## Usage

### After each real drive:
```bash
# Run comparison test
./test_ekf.sh [duration] --gyro

# Check output file
ls -lh ~/gojo/motion_tracker_sessions/comparison_*.json.gz

# Add entry to this tracker manually or run analysis script
python3 analyze_comparison.py ~/gojo/motion_tracker_sessions/comparison_LATEST.json.gz
```

### Track trends:
```bash
# After 5+ drives, analyze overall pattern
python3 analyze_comparison.py ~/gojo/motion_tracker_sessions/
```

---

## Expected Outcomes

### If EKF consistently wins:
✅ Validates 13D bias-aware EKF decision
✅ Justifies the added complexity
✅ Can confidently make EKF the default in motion_tracker_v2
✅ Document the performance gap for open-source release

### If Complementary is close:
⚠️ Might consider simpler filter (Occam's Razor)
⚠️ Could keep both as options with user choice
⚠️ Would reduce maintenance burden

### If performance varies by conditions:
🔍 Identify when EKF shines (GPS loss? high dynamics?)
🔍 Could use conditional filter selection
🔍 Valuable insight for production deployment

---

## Long-Term Analysis Goals

1. **Over 10 drives:** See if advantage is consistent
2. **Over 30 drives:** Identify patterns by route type
3. **Over 100 drives:** Understand edge cases and failure modes
4. **Publication quality:** Enough data for research paper/documentation

---

## Decision Criteria

After sufficient data collection:

**Choose EKF if:**
- Consistently outperforms Complementary (>2-3% advantage)
- Advantage holds across different conditions
- Incident detection more reliable
- Memory/CPU overhead acceptable (it is - 92 MB)

**Consider alternatives if:**
- Advantage is marginal (<1%)
- Complementary wins in common scenarios
- EKF shows instability in real-world conditions
- Maintenance burden too high

---

**Current Status:** Baseline tracking started
**Next Step:** Run real drives and log results
**Review Point:** After 5-10 drives for initial pattern analysis

