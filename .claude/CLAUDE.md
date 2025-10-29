# Gojo Project Overview & Session Notes

## Project Status
**General Playground:** Various sensor fusion, motion tracking, and system monitoring experiments. Single Termux working directory with multiple tools.

**Philosophy:** Keep related projects in one workspace. Each tool independent, can be developed/tested in separate Claude Code sessions.

**Current Priority:** Motion Tracker V2 (production-ready sensor fusion)

---

## 👤 Working Style & Process

**I am not a programmer.** I direct complex technical projects by:
- Understanding problem domains deeply (what needs solving, not how to code it)
- Asking detailed questions about technical tradeoffs and architecture
- Catching logical and system-level errors (not syntax errors)
- Testing rigorously to validate requirements are met
- Making decisions based on system goals and constraints

**How I work with AI (Claude Code):**
1. Describe the goal/problem clearly
2. Ask clarifying questions if proposals don't make sense
3. Guide solutions through feedback and direction (not implementation details)
4. Validate through testing before accepting work as done
5. Commit explicitly when goals are achieved

**What helps in sessions:**
- Explain *why* technical choices matter (tradeoffs, not just code)
- Show test output or demos to validate solutions work
- Make assumptions explicit (ask "does this match your goal?")
- Document decisions clearly (helps future sessions)
- Ask if I understand before proceeding with implementation

**What I expect to understand:**
- The system architecture (how pieces fit together)
- Performance metrics (accuracy, speed, reliability)
- Failure modes (what can go wrong, how we detect it)
- Testing strategy (how to validate it works)
- NOT: Detailed math, algorithm internals, or syntax specifics

**Example from Oct 29 session:**
- Built production-grade Kalman filters (EKF/UKF) by directing the solution
- Caught critical bugs by understanding system goals (gyro integration, sensor initialization)
- Designed real-time comparison framework for validation
- Did NOT need to write code or understand quaternion math - just guided what code should accomplish

---

## 🏆 Priority Projects & Wins

### Motion Tracker V2 - Open Source Incident Logger (ACTIVE)
**Status:** ✓ Production Ready | Direction: Open-Source Privacy/Insurance Tool

**Mission:** DIY incident detection for privacy-conscious drivers
- Log hard braking, impacts, swerving with independent sensor data
- Prove what happened in accident disputes without corporate trackers
- Open source, community-driven, transparently validated

**Location:** `motion_tracker_v2/`
- `motion_tracker_v2.py` - Main application + incident detection
- `filters/` - Sensor fusion engines (EKF, UKF, Complementary, Kalman)
- `test_ekf_vs_complementary.py` - Real-time filter comparison framework
- `analyze_comparison.py` - Post-drive analysis and quality scoring
- `accel_processor.pyx` - Cython optimization (25x faster)

**Sensor Fusion Stack:**
- **Extended Kalman Filter (EKF)** - Primary (9.5/10) - handles non-linear GPS + gyro
- **Unscented Kalman Filter (UKF)** - Alternative (8.0/10) - sigma-point based
- **Kalman (Pure NumPy)** - Reference (9.0/10) - no external dependencies
- **Complementary Filter** - Baseline (7.5/10) - fast, simple fusion
- All use Joseph form covariance for numerical stability

**Features:**
- GPS + Accelerometer + Gyroscope sensor fusion (multi-filter comparison)
- Automatic incident detection (hard braking >0.8g, impacts >1.5g, swerving >60°/sec)
- Kalman-filtered data (accurate, noise-reduced)
- 50 Hz accelerometer sampling with optional Cython acceleration
- Dynamic recalibration during stationary periods (handles phone rotation)
- Auto-save every 2 minutes with memory management
- Real-time filter validation framework
- Battery monitoring and session summaries
- Exports: JSON (raw + filtered), CSV, GPX formats

**Run:**
```bash
# MAIN TRACKER (Standard logging with EKF filter)
./motion_tracker_v2.sh 5                    # Run for 5 minutes (default: EKF)
./motion_tracker_v2.sh --filter=complementary 10  # 10 min with complementary filter
./motion_tracker_v2.sh --enable-gyro 5     # 5 minutes with gyroscope

# TEST/COMPARISON (EKF vs Complementary real-time validation)
# ⚠️ CRITICAL: ALWAYS use shell script, NOT direct Python
./test_ekf.sh 10                           # 10-minute test (EKF vs Complementary)
./test_ekf.sh 5 --gyro                     # 5 minutes with gyro comparison

# Analysis & Results
python motion_tracker_v2/analyze_comparison.py comparison_*.json
```

⚠️ **CRITICAL: Shell Script is MANDATORY for test_ekf.sh**
- Direct Python: `python test_ekf_vs_complementary.py` → Accelerometer sensor fails
- Shell script: `./test_ekf.sh` → Properly initializes and manages sensor environment
- Shell script handles:
  1. Cleanup of stale sensor processes from previous runs
  2. 3-second delay for sensor resource release
  3. Clean subprocess initialization with proper signal handling
  4. Final cleanup after test completion

**Data:** Saves to `motion_tracker_sessions/` (incidents in separate folder)

**Oct 29 Session Additions:**
- ✓ Extended Kalman Filter with 10D quaternion state (GPS+Accel+Gyro)
- ✓ Real-time dual-filter comparison framework
- ✓ Post-test analysis with accuracy scoring
- ✓ Startup validation (10-second sensor warmup + MANDATORY accelerometer data check)
- ✓ Incident detection module (hard braking, impacts, swerving)

**CRITICAL TEST VALIDATION RULE:**
```
🚨 SHELL SCRIPT IS MANDATORY:
   WRONG: python motion_tracker_v2/test_ekf_vs_complementary.py 10
   RIGHT: ./test_ekf.sh 10

   Using direct Python will NOT initialize the sensor properly.
   Always use ./test_ekf.sh instead.

⚠️  A test is ONLY VALID if:
  1. Accelerometer data is received within 10 seconds of startup
  2. At least 1+ accelerometer samples collected during entire test run
  3. If test shows "Accel samples: 0" → TEST IS INVALID, do not declare success

✗ Failure scenarios (DO NOT ignore):
  - "Accel samples: 0" after test completes = sensor issue, not test success
  - "No accelerometer data after 10 seconds" at startup = fails immediately

✓ Test passes only with actual accelerometer data in output

⚠️  STALE SENSOR PROCESSES:
   If test_ekf.sh fails with "No accelerometer data received":
   1. Script already handles cleanup, but might need manual reset
   2. Verify: termux-sensor -s ACCELEROMETER (should show JSON output)
   3. Manual cleanup: pkill -9 termux-sensor && pkill -9 termux-api && sleep 3
   4. Then retry: ./test_ekf.sh 10
```

**Next Steps:**
- Validate EKF on real drive with accelerometer enabled
- Create calibration + legal use documentation
- Prepare for open source release

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
├── .claude/CLAUDE.md                 (This file)
├── motion_tracker_v2.sh              (Launch wrapper)
├── motion_tracker_v2/                (Priority: main code)
│   ├── motion_tracker_v2.py          (Main application)
│   ├── accel_processor.pyx           (Cython source)
│   ├── accel_processor.cpython-312.so (Compiled module)
│   └── setup.py                      (Build config)
├── motion_tracker_sessions/          (Priority: data storage)
│   ├── motion_track_v2_*.json        (Raw data)
│   ├── motion_track_v2_*.json.gz     (Compressed)
│   └── motion_track_v2_*.gpx         (Maps format)
└── [other tools/experiments...]
```

---

## 🚀 Quick Start

**For next session with motion tracker:**
```bash
cd ~/gojo
python motion_tracker_v2/motion_tracker_v2.py  # Continuous mode
# or
./motion_tracker_v2.sh 10                      # 10 minute run
```

**Check last session:**
```bash
ls -lh motion_tracker_sessions/ | tail -3
```

**Analyze data:**
```bash
gunzip -c motion_tracker_sessions/*.json.gz | python3 -m json.tool | less
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
