# Gojo Motion Tracker V2 - Project Reference

## Latest: Nov 12, 2025 - Gyro Resolution Fixed + Architecture Issue Documented

**Status:** ✅ WORKING (Gyro now matches accel resolution ~1200 samples/min)

### Gyro Sample Count Fixed
**Problem:** Gyro collected only 59 samples vs 1216 accel samples (102x discrepancy)
**Root Cause:** `time.sleep(0.01)` in gyro_loop limited processing to 100 Hz
**Solution:** Removed sleep bottleneck, added comprehensive logging
**Result:** Gyro 1242 samples vs Accel 1216 (matching resolution ✓)

**Files Modified:**
- `test_ekf_vs_complementary.py` lines 837-853: Removed sleep(0.01), added logging

---

### Architecture Issue: Filter Blocking (Refactor Scheduled)
**Current Problem:** Filters run synchronously in data collection loops - if any filter hangs (e.g., ES-EKF), ALL data collection stops
**User Vision:** "we should be getting that base data from gps, accel, gyro, then the filters do their independent things"
**Proposed:** Decouple raw data collection from filter processing via independent filter threads consuming from raw data queues
**Status:** 📋 Detailed plan in ARCHITECTURE_REFACTOR_PLAN.md (6 phases, ~85 min implementation, scheduled for tomorrow)

---

## Nov 11, 2025 - P1 Critical: Sensor Daemon Stability Fixed + Dashboard Speedometer Redesigned

### Dashboard Improvement: Speedometer Redesigned
**Problem:** Rotating needle tachometer visually confusing (appeared in wrong speed zone at 0 km/h)
**Solution:** Changed to horizontal slider moving left-to-right across green/yellow/red zones, 8px red slider, smooth transitions
**Files Modified:**
- `dashboard_server.py` lines 779-909: CSS redesign (conic-gradient → linear speed zones)
- `dashboard_server.py` lines 1031-1054: HTML structure with speed markers (0, 40, 80, 120, 160, 200+ km/h)
- `dashboard_server.py` lines 1204-1207: JS positioning (rotate() → left: X%)

---

### P1 Critical: Sensor Daemon Death Loop Fix
**Problem:** Tests failed within 5-10 minutes due to cascading daemon restarts

**Root Causes & Fixes:**
1. **Zombie processes** - Added pgrep polling loop (5s wait, 200ms intervals) before daemon restart
2. **FD leaks** - Added `close_fds=True` + explicit stdout/stderr/stdin cleanup in finally blocks (5-10 FDs leaked per restart → 1024 limit hit after ~100 restarts)
3. **Race conditions** - Added `threading.Lock` + double-check pattern to prevent health monitor + status logger concurrent restarts
4. **Termux:API exhaustion** - Extended cooldown 10s → 12s (socket pool ~10 concurrent, TCP TIME_WAIT blocks reuse)
5. **Validation timeout** - Extended 15s → 30s initial + 5s delay + 10s retry (Android sensor backend needs 20-30s post-crash)

**Files Modified:**
- `test_ekf_vs_complementary.py`: Lines 348-350 (added locks), 958-1042 (_restart_accel_daemon), 1044-1127 (_restart_gps_daemon), 144 (close_fds), 235-258 (GPS stop)
- `motion_tracker_v2.py`: Lines 168, 408 (close_fds for accel/gyro), 272-302 (accel stop), 589-617 (gyro stop)

**Critical Bug Found During Code Review:** Line 1093 had `pgrep -x python3` (wrong - matches test script itself), fixed to `pgrep -x termux-location` (correct - actual GPS process)

**Expected Impact:** 5-10 min → 20-30+ min stable (pending validation with real GPS + motion)

---

## Historical Fixes Summary

**Nov 12:** Gyro resolution fixed - Removed time.sleep(0.01) bottleneck → 59 samples → 1242 samples (matches accel ~1200/min)
**Nov 6:** Memory optimization - Clear deques after auto-save (not accumulated_data) → 70 MB freed, bounded 92-112 MB stable
**Nov 6:** GPS polling fixed - Changed blocking subprocess.run(timeout=15s) → non-blocking async poller (0.38 Hz → 0.9-1.0 Hz)
**Nov 4:** Incident detection validated - 25-min test: 126 swerving events (5.04/min, all real), 71% false positive reduction via motion context
**Nov 4:** Gyroscope stabilized - Removed stdbuf wrapper (breaks Termux:API socket IPC), use bufsize=1 instead
**Nov 3:** Critical bugs fixed - Data loss (accumulated_data), race conditions (locks), deque overflow, physics violation, health monitor race

See GPS_ROADMAP.md for GPS improvement research & roadmap (Tiers 1-3).

---

## Motion Tracker V2 - Status

**Goal:** Privacy-focused incident logger for drivers (open-source)
**Location:** `motion_tracker_v2/`
**Core Features:** EKF filter (primary), Complementary (fallback), UKF, Kalman fusion; Hard braking (>0.8g), impacts (>1.5g), swerving (>60°/sec with motion context); GPS ~0.2 Hz, Accel ~19 Hz, Gyro paired; Bounded 92-112 MB; Cython 25x optional; JSON/CSV/GPX/Incident logs

### Hardware
- Device: Samsung Galaxy S24 (Termux on Android 14)
- IMU: LSM6DSO (accel + gyro paired)
- GPS: LocationAPI

### Run Commands
```bash
# Standard tracking (EKF filter)
./motion_tracker_v2.sh 5                         # 5 minutes
./motion_tracker_v2.sh --enable-gyro 5           # With gyroscope
./motion_tracker_v2.sh --filter=complementary 10 # Complementary filter

# Test/Comparison (⚠️ MUST use shell script, not direct Python)
./test_ekf.sh 10        # EKF vs Complementary (10 min)
./test_ekf.sh 5 --gyro  # With gyro included
./test_ekf.sh           # Continuous mode (Ctrl+C to stop)

# Analysis
python motion_tracker_v2/analyze_comparison.py comparison_*.json
```

### Operating Modes
**Timed:** `./test_ekf.sh 5` - Runs 5 min, auto-saves every 15 sec, final save combines accumulated_data + deques
**Continuous:** `./test_ekf.sh` - Runs until Ctrl+C, memory stays 92-112 MB (deques cleared after auto-save)

### Critical Rules
**NEVER run direct Python:**
- ✗ `python test_ekf_vs_complementary.py` (sensor init fails)
- ✓ `./test_ekf.sh 10` (shell handles cleanup + init)

**Test validity requires:** Accel data within 10s, ≥1 accel sample, never "Accel samples: 0"
**Stale sensor recovery:** `pkill -9 termux-sensor && pkill -9 termux-api && sleep 3 && ./test_ekf.sh 5`

---

## Dashboard Server (FastAPI Backend)

**Launch:** `cd ~/gojo && ./start_dashboard.sh` → http://localhost:8000

**API Endpoints:**
- `/` - Main dashboard (list all drives with map previews)
- `/api/drives` - JSON list of sessions
- `/api/drive/{drive_id}` - Detailed metrics + GPX export
- `/live` - Live session monitoring
- `/api/live/status` - Real-time status feed (updated every 2s)

**Features:** Interactive Leaflet.js maps, session stats (distance/samples/memory/speed), metadata caching, GPX export, real-time dashboard

---

## Technical Config

### EKF Filter Tuning (Nov 4)
- GPS noise std dev: 8.0 m (was 5.0m) - Matches 3-second GPS gap accumulation
- Accel process noise: 0.3 m/s² (was 0.1 m/s²) - Prevents integration drift between fixes
- Expected distance error: 12.19% → ~4-6%

### Sensor Sampling (Actual Hardware Rates)
- Accel: 19 Hz (nominal 50 Hz, Python threading overhead)
- GPS: 0.2 Hz (5-second polling interval, Termux:API limit)
- Gyro: Paired with accel (~19 Hz, same IMU chip)

### Auto-Save & Live Monitoring
- **Auto-Save Interval:** Every 15 seconds (appends to accumulated_data, clears deques)
- **Startup:** Gravity calibration (20 stationary samples ~3s) removes gravity orientation-independently
- **Live Status File:** `motion_tracker_sessions/live_status.json` (updated every 2s) for dashboard monitoring
- **Metrics Export:** `metrics_*.json` (when --gyro enabled) tracks EKF health metrics

### Incident Detection Thresholds
| Event | Threshold | Context Filtering |
|-------|-----------|-------------------|
| Hard Braking | >0.8g | None |
| Impact | >1.5g | None |
| Swerving | >60°/sec | GPS speed >2 m/s + 5sec cooldown |

**Smart Filtering:** 25-min test: 259 events (no filter) → 126 events (71% reduction) via motion context (GPS speed + yaw + cooldown)

### Sensor Health Monitoring
- **Accel silence threshold:** 5 seconds (auto-restart)
- **GPS silence threshold:** 30 seconds (auto-restart, test continues without GPS)
- **Max restart attempts:** 60 per sensor
- **Restart cooldown:** 10 seconds (allows resource release)

---

## File Structure
```
gojo/
├── .claude/CLAUDE.md                  (This file - lean reference)
├── .claude/GPS_ROADMAP.md             (GPS research & Tier roadmap)
├── motion_tracker_v2.sh               (Launch wrapper)
├── test_ekf.sh                        (Test wrapper - MANDATORY)
├── dashboard_server.py                (FastAPI backend, 2,230 lines)
├── motion_tracker_v2/
│   ├── motion_tracker_v2.py           (Main app)
│   ├── filters/                       (EKF, UKF, Kalman, Complementary)
│   ├── test_ekf_vs_complementary.py   (Comparison test)
│   ├── analyze_comparison.py          (Post-test analysis)
│   └── incident_detector.py           (30s context capture)
└── motion_tracker_sessions/
    ├── comparison_*.json              (Raw data)
    ├── live_status.json               (Real-time monitoring)
    └── incidents/                     (Incident logs)
```

---

## Quick Start

**Monitor live test in real-time:**
```bash
cd ~/gojo && ./test_ekf.sh 20 &
watch -n1 'tail -5 motion_tracker_sessions/live_status.json | python3 -m json.tool | grep -E "accel_samples|gps_fixes|velocity|memory_mb"'
```

**Analyze test results:**
```bash
python3 motion_tracker_v2/analyze_comparison.py motion_tracker_sessions/comparison_*.json
```

**Key metrics to watch:**
- Distance error: < 10% (target: < 5%)
- EKF vs Complementary: 0.0% (both use GPS)
- Velocity std dev: < 12 m/s (smoothness)
- Memory: 92-100 MB stable (no growth)
- Gyro samples: Continuous increase (daemon health)

---

## Termux-Specific Quirks

- `/tmp/` files don't persist - Use `~/gojo/logs/` for persistent logging
- `stdbuf -oL` breaks socket IPC - Never use in subprocess wrappers (use `bufsize=1` instead)
- Sensor cleanup mandatory - Use shell scripts, not direct Python
- `pkill -9` is asynchronous - Must poll pgrep to verify actual process exit

---

## Key Learnings from Nov 11 Session (Daemon Stability Work)

### 1. Process Lifecycle in Linux/Android
- **`pkill -9` is NOT synchronous** - Kernel reaping delay 500ms-2s on Android → must poll pgrep
- **Without polling:** Zombie processes hold file descriptors, causing cascading failures

### 2. File Descriptor Management
- **Android FD limit:** ~1024 per process
- **FD leak pattern:** subprocess.Popen inherits parent's FDs by default (Android quirk)
- **Fix:** `close_fds=True` + explicit cleanup in finally blocks (stdout, stderr, stdin)
- **Accumulation:** 5-10 FDs per restart → hit 1024 limit after ~100 restarts

### 3. Thread Race Conditions
- **Scenario:** Health monitor (T=0s) + status logger (T=2s) both detect daemon death
- **Problem:** Both attempt restart simultaneously → resource conflicts
- **Fix:** `threading.Lock` + double-check pattern (acquire lock → verify inside lock)
- **Key:** Lock must wrap ENTIRE detection+action sequence

### 4. Termux:API Backend Limitations
- **Socket pool:** ~10 concurrent connections
- **TCP TIME_WAIT:** Prevents immediate socket reuse after close
- **Result:** Rapid restart cycles exhaust pool → "Connection refused" errors
- **Fix:** Extended cooldown (12s total) allows socket cleanup

### 5. Process State vs Data Availability
- **Process running:** subprocess.poll() == None ✓
- **Process actually producing data:** Requires validation attempt, not just existence check
- **Gap:** GPS daemon could be running but hung in termux-location call
- **Solution:** All health checks include actual data attempt (not just process check)

### 6. Process Name Matching Precision
- **WRONG:** `pgrep python3` → matches test script + any Python process (silent failure)
- **CORRECT:** `pgrep -x termux-location` → only GPS wrapper process
- **Impact:** Broad patterns in system automation = silent cascading failures
- **Code Review Win:** Sonnet caught `pgrep python3` before testing (would have broken zombie cleanup)

### 7. Implementation vs Logic Correctness
- **Syntax validity:** Code may compile/run without errors
- **Logic correctness:** Pattern matching with broad names defeats purpose silently
- **Lesson:** Process automation requires exact/explicit matching (pgrep -x, not fuzzy patterns)

### 8. Daemon Health = "Alive" + "Working"
- **"Alive":** Process exists (subprocess.poll() == None)
- **"Working":** Process produces data (actual data retrieval succeeds)
- **Key insight:** Validation must attempt actual operation, not assume activity from existence

---

## Incident Detection & Context Capture

**Format:** JSON with 30-second windows (before + after event)
**Location:** `motion_tracker_sessions/incidents/`
**Data per incident:** ~590 accel samples (30s @ 18 Hz), ~590 gyro samples, ~10 GPS samples, event magnitude/timestamp, vehicle speed context

**Access:**
```bash
ls ~/gojo/motion_tracker_sessions/incidents/
python3 -c "import json; print(json.load(open('incident_*.json')))"
```

---

## Expected Performance

- **Startup:** 85MB → 92MB (5s + 3s gravity calibration)
- **CPU:** 15-25% tracking, 30-35% with metrics
- **Memory:** 92-112MB stable (no growth despite 15s auto-saves)
- **Battery:** ~8-10%/hour
- **Data:** ~0.5-1 MB per 15sec auto-save

---

## Troubleshooting

### No Accelerometer Data
```bash
pkill -9 termux-sensor && pkill -9 termux-api && sleep 3
./test_ekf.sh 5 --gyro
```

### Memory Growing
- Expected: 92 MB ± 2 MB
- If growing: auto-save failed → restart test

### GPS Timeout
- Expected: 5-30s for first lock
- If sustained: LocationAPI issue → system degrades to inertial-only

### Disk Space
```bash
df -h | grep "storage/emulated"  # Should show 250+ GB free
```

---

## Architecture

**3-Layer + Dashboard:**
1. **Data Collection** (_accel_loop, _gps_loop, _gyro_loop) - Fast, non-blocking
2. **Persistence** (_save_results) - Auto-save every 15s, clears deques to bound memory
3. **Health Monitoring** (_health_monitor_loop) - Runs every 2s, detects & recovers daemon failures async
4. **Dashboard** (live_status.json, display_loop) - Real-time monitoring file + metrics

**Key Insights:**
- No blocking operations in data collection path
- Health monitor is single source of truth for daemon restarts
- Live status file enables external dashboard integration
- Memory bounded by 15s auto-save + deque clearing regardless of test duration

---

## Code Patterns (Brief Reference)

1. **Complementary Filtering** (motion_tracker_v2.py:75-128) - GPS (70%) corrects accel drift, accel (30%) high-freq detail
2. **Magnitude-Based Calibration** (motion_tracker_v2.py:354-433) - Remove gravity orientation-independently
3. **Cython with Auto-Fallback** (motion_tracker_v2.py:25-30, 611-649) - Try import, fallback to Python (25x speedup optional)
4. **Thread-Safe State** (motion_tracker_v2.py:32-180) - threading.Lock + get_state() for atomic reads
5. **Bounded Memory Deques** (motion_tracker_v2.py:547-552, 662-670) - `deque(maxlen=N)` + clear on auto-save
6. **Paired Sensor Init** (motion_tracker_v2.py:143-360) - Single process, dual queues (accel + gyro from same IMU chip)
7. **Zombie Process Reaping** (test_ekf_vs_complementary.py:1093-1103) - pgrep polling loop validates actual process exit
8. **Lock Pattern for Thread Safety** (test_ekf_vs_complementary.py:348-350, 958-1042) - Acquire lock → double-check condition inside lock

---

## Deprecated/Replaced Patterns

- ❌ `subprocess.run(timeout=15s)` - Caused thread starvation, replaced with non-blocking async polling
- ❌ `stdbuf -oL termux-sensor` - Breaks Termux:API socket IPC, use `bufsize=1` instead
- ❌ `python test_ekf_vs_complementary.py` - Sensor init fails, use `./test_ekf.sh` shell wrapper
- ❌ Zombie process cleanup without polling - Use pgrep polling loop to verify actual exit

---

## Working Style

**I guide through:**
- Understanding problem domains (what, not how)
- Asking about tradeoffs and architecture
- Catching logical/system errors (not syntax)
- Rigorous testing before accepting solutions

**Expect me to understand:**
- System architecture (how pieces fit)
- Performance metrics (accuracy, speed, reliability)
- Failure modes (what breaks, how we detect)
- Testing strategy (validation approach)
- NOT: Math details, algorithm internals, syntax specifics

---

## Next Steps

1. **Run extended test with real GPS + motion data** (vehicle environment)
2. **Monitor stability metrics** (process count, FD count, restart attempts)
3. **Plan Tier 1 GPS improvements** (see GPS_ROADMAP.md)
4. **Validate heap/memory bounds** (20-30+ min test without death loop)
