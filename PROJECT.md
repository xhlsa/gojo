# Gojo Project Overview & Session Notes

## Project Status
**General Playground:** Various sensor fusion, motion tracking, and system monitoring experiments. Single Termux working directory with multiple tools.

**Philosophy:** Keep related projects in one workspace. Each tool independent, can be developed/tested in separate Claude Code sessions.

**Current Priority:** Motion Tracker V2 (production-ready sensor fusion)

---

## 🏆 Priority Projects & Wins

### Motion Tracker V2 (ACTIVE)
**Status:** ✓ Production Ready | Latest Commit: `72d83b0`

**Location:** `motion_tracker_v2/`
- `motion_tracker_v2.py` - Main application
- `accel_processor.pyx` - Cython optimization (25x faster)
- `setup.py` - Build configuration

**Features:**
- GPS + Accelerometer sensor fusion (complementary filtering)
- 50 Hz accelerometer sampling with optional Cython acceleration
- Dynamic re-calibration during stationary periods (handles phone rotation)
- Auto-save every 2 minutes with memory management
- Battery monitoring and session summaries
- Exports: JSON, compressed JSON.GZ, GPX formats

**Run:**
```bash
python motion_tracker_v2/motion_tracker_v2.py [duration_minutes]  # e.g., python motion_tracker_v2/motion_tracker_v2.py 5
./motion_tracker_v2.sh [duration]                                 # Wrapper script from root
```

**Data:** Saves to `sessions/2025-10-26_orjson_integration/` (date-based organization)

**Recent Changes (Oct 26-27):**
- ✓ Integrated orjson (2-3x JSON performance improvement)
- ✓ Reorganized sessions into date-based folders for multi-session clarity
- ✓ Improved accelerometer daemon startup reliability (retries + delays)
- ✓ Moved PROJECT.md to root from .claude/ for visibility
- Current: Investigating accelerometer data collection with `stdbuf -oL` subprocess pattern

---

## 📋 Successful Code Patterns

### 1. **Complementary Filtering for Sensor Fusion**
**Pattern Used:** Motion Tracker V2 (lines 75-168)

```
Core idea: GPS corrects accel drift, accel provides high-frequency detail
- GPS: Low frequency (~1/sec), low noise, absolute position truth
- Accel: High frequency (50 Hz), drifts over time, good for transients

Implementation:
  1. GPS update → velocity = GPS_velocity (absolute correction)
  2. Accel samples → velocity += accel_magnitude * dt (temporal detail)
  3. Weighting: 70% GPS, 30% accel (tunable)
  4. Drift correction: Reset accel_velocity to fused velocity on each GPS update
```

**When to use:** Fusing slow/accurate + fast/noisy sensors
**Reuse file:** motion_tracker_v2.py:75-128 (SensorFusion.update_gps/update_accelerometer)

---

### 2. **Magnitude-Based Calibration (Orientation-Independent)**
**Pattern Used:** Motion Tracker V2 (lines 354-433)

```
Core idea: Remove gravity by magnitude, not axis-by-axis
- Problem: Device rotates → x/y/z biases become stale
- Solution: Use magnitude of acceleration vector (gravity is always |g|)

Implementation:
  1. Calibration: Collect N stationary samples
     - Per-axis bias: mean of samples (x_bias, y_bias, z_bias)
     - Gravity magnitude: sqrt(x_bias² + y_bias² + z_bias²) ≈ 9.81
  2. During tracking:
     - Raw magnitude = sqrt(x² + y² + z²)
     - Motion magnitude = raw_magnitude - gravity_magnitude
     - Result: Works at ANY orientation (no recalibration needed)
  3. Dynamic recal: If stationary >30sec, recollect samples and update
```

**When to use:** Accelerometer needs to work in any orientation
**Reuse files:** motion_tracker_v2.py:354-433 (calibrate, try_recalibrate methods)

---

### 3. **Cython Optimization with Automatic Fallback**
**Pattern Used:** Motion Tracker V2 (lines 25-30, 611-649)

```
Core idea: Try fast path first, fallback to pure Python gracefully
- Problem: Cython .so file may not exist on import
- Solution: Try/except with feature detection

Implementation:
  1. At import time:
     try:
       from accel_processor import FastAccelProcessor
       HAS_CYTHON = True
     except ImportError:
       HAS_CYTHON = False

  2. At runtime, check flag and use appropriate path:
     if HAS_CYTHON:
       use FastAccelProcessor (pre-compiled .so)
     else:
       use AccelerometerThread (pure Python)

  3. Result: 25x speedup if compiled, no crashes if missing
```

**When to use:** Need performance but must work without optional deps
**Reuse files:** motion_tracker_v2.py:25-30, 611-649

---

### 4. **Thread-Safe State with Lock + Get State Method**
**Pattern Used:** Motion Tracker V2 (lines 32-180)

```
Core idea: Thread-safe read/write with minimal locking
- Problem: Main thread + GPS thread + Accel thread all modify state
- Solution: Explicit lock + atomic get_state() method

Implementation:
  1. Class has self.lock = threading.Lock()
  2. All writes protected: with self.lock: modify_state()
  3. All reads go through get_state():
     def get_state(self):
       with self.lock:
         return {
           'velocity': self.velocity,
           'distance': self.distance,
           'is_stationary': self.is_stationary
         }
  4. Threads call state = fusion.get_state() (non-blocking read)
  5. Prevents race conditions on critical data
```

**When to use:** Multiple threads modifying shared state
**Reuse file:** motion_tracker_v2.py:32-180 (SensorFusion class)

---

### 5. **Bounded Memory with Deques + Auto-Clear**
**Pattern Used:** Motion Tracker V2 (lines 547-552, 662-670)

```
Core idea: Prevent unbounded memory growth with fixed-size circular buffers
- Problem: Long-running app collects infinite samples
- Solution: Use deque(maxlen=N) + periodic clear after save

Implementation:
  1. At init:
     self.samples = deque(maxlen=10000)  # Max 10k GPS samples
     self.accel_samples = deque(maxlen=50000)  # More for 50 Hz data
  2. During auto-save:
     self.save_data(auto_save=True, clear_after_save=True)
     # This saves to file, then clears in-memory deques
  3. Result: Memory stays bounded regardless of session length

  Note: For very long sessions, still need manual cleanup but prevents
        catastrophic runaway memory issues
```

**When to use:** Streaming data collection with unbounded input
**Reuse files:** motion_tracker_v2.py:547-552, 662-670

---

### 6. **Stationary Detection with Threshold Hysteresis**
**Pattern Used:** Motion Tracker V2 (lines 101-107)

```
Core idea: Detect when device stops moving reliably
- Problem: GPS noise creates false motion signals
- Solution: Multi-threshold approach

Implementation:
  1. Two conditions (AND logic):
     movement_threshold = max(5.0, gps_accuracy * 1.5)
       # If GPS says ±5m uncertainty, we need >7.5m movement to register
     speed_threshold = 0.1  # m/s (~0.36 km/h)
       # If GPS speed <0.1 m/s, likely stopped

  2. is_stationary = (distance_moved < movement_threshold) AND
                     (gps_velocity < speed_threshold)

  3. Use for:
     - Dynamic recalibration (collect samples while still)
     - Zero velocity indication (not just low velocity)
     - Avoid false positives from GPS jitter
```

**When to use:** GPS-based motion detection needs to filter noise
**Reuse file:** motion_tracker_v2.py:101-107

---

## 🔧 Technical Decisions

### GPS + Accel Fusion Weights
- GPS: 70% weight (accurate but low frequency)
- Accel: 30% weight (noisy but high frequency)
- Can tune based on GPS accuracy (auto-weight in future?)

### Accelerometer Sampling
- Default: 50 Hz (good balance of detail vs CPU load)
- Cython: 25x faster at same rate (70% CPU reduction)
- Could go higher (100+ Hz) if CPU permits

### Auto-Save Interval
- 2 minutes: Good balance for long drives
- Prevents data loss without excessive disk I/O
- Memory cleared after each save to prevent overflow

### Dynamic Recalibration
- Trigger: Stationary for 30+ seconds
- Frequency: Check every 30 seconds max
- Threshold: Only log if gravity drift > 0.5 m/s² (significant change)
- Result: Auto-corrects for phone rotation, silent for minor drift

---

## 📚 Future Improvements (If Needed)

1. **Adaptive GPS Weighting:** Auto-adjust 70/30 split based on GPS accuracy
2. **Kalman Filter:** Replace complementary filter for better fusion
3. **Altitude Tracking:** Use GPS altitude + pressure sensor
4. **Trip Analysis:** Segment drives into acceleration/cruise/deceleration
5. **Web Dashboard:** Real-time session monitoring
6. **SQLite Backend:** Replace JSON files for query capability

---

## 🗂️ Project Structure

```
gojo/
├── PROJECT.md                        (This file - project overview)
├── .claude/                          (Claude Code settings)
├── motion_tracker_v2.sh              (Launch wrapper)
├── motion_tracker_v2/                (Active code)
│   ├── motion_tracker_v2.py          (Main application)
│   ├── accel_processor.pyx           (Cython optimization)
│   ├── accel_processor.cpython-312.so (Compiled module)
│   ├── accel_health_monitor.py       (Sensor diagnostics)
│   └── setup.py                      (Build config)
├── sessions/                         (Date-based session organization)
│   ├── 2025-10-23_initial_tests/     (Early development)
│   ├── 2025-10-24_kalman_exploration/ (Kalman filter experimentation)
│   ├── 2025-10-25_refactoring/       (Code organization)
│   ├── 2025-10-26_orjson_integration/ (JSON optimization + accel debugging)
│   ├── 2025-10-27_accel_fix/         (Current session)
│   └── archive/                      (Old/reference data)
├── motion_tracker_kalman/            (Alternative Kalman implementation)
├── docs/                             (Documentation)
├── tests/                            (Unit tests)
├── tools/                            (Utilities)
└── [other tools/experiments...]
```

**Session Organization:** Each date folder contains all outputs from that Claude session. Separates iterations to avoid context clutter when switching between sessions.

---

## 🚀 Quick Start

**For any session with motion tracker:**
```bash
cd ~/gojo
python motion_tracker_v2/motion_tracker_v2.py  # Continuous mode
# or
./motion_tracker_v2.sh 10                      # 10 minute run

# Data automatically saves to: sessions/YYYY-MM-DD_session/
```

**Check today's session data:**
```bash
ls -lh sessions/2025-10-27*/
```

**Analyze specific session:**
```bash
gunzip -c sessions/2025-10-26_orjson_integration/*.json.gz | python3 -m json.tool | less
```

**List all sessions:**
```bash
ls -1d sessions/*/
```

---

---

## 📁 Other Tools in This Workspace

| Tool | Purpose | Status |
|------|---------|--------|
| `motion_tracker.py` | Original motion tracker (v1) | Legacy |
| `motion_tracker_benchmark.py` | Performance testing & benchmarking | Utility |
| `system_monitor.py` | Termux system stats & telemetry | Active |
| `ping_tracker.py` | Network ping tracking | Utility |
| `ping_tracker_enhanced.py` | Enhanced ping analysis | Utility |
| `gps_tester.py` | GPS functionality validation | Testing |
| `monitor_ping.sh` | Simple ping monitoring script | Utility |

These are separate experiments in the same workspace. Focus on Motion Tracker V2 for production use; others can be picked up in dedicated sessions if needed.

---

## 📝 Session Log

### Oct 23 (Today)
- ✓ Added dynamic re-calibration for accelerometer drift
- ✓ Tested: 2min highway, 5min indoor, 3min folder-structure tests
- ✓ Reorganized code into dedicated project folder
- ✓ All tests passing, ready for real drive session
- ✓ Created comprehensive documentation (README.md + CLAUDE.md)

**Next:** Run during actual car drive to validate dynamic recal during traffic stops
