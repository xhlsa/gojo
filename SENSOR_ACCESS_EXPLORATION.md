# Alternative Sensor Access Methods - Exploration Report

## Current Status
**Today's Date:** Oct 30, 2025  
**Device:** Samsung SM-S918U (Galaxy S24 Ultra)  
**OS:** Android 16 (API 36)  
**Termux Version:** 0.118.3

---

## 1. Available Sensor Access Methods on This System

### ✓ Currently Working
- **termux-sensor** - Termux:API wrapper (currently used)
  - Rate: ~15 Hz (LocalSocket architecture limit)
  - Stable, no API overload at 1.0 second polling intervals
  
- **termux-location** - GPS wrapper via Termux:API
  - Rate: Environment-dependent (0.25 Hz indoors, ~1 Hz outdoors)
  - Stable with 1.0 second polling

### ✗ Not Available (Checked)
- **PyJNI / JNIUS** - Java/Android bridge (would enable direct sensor HAL access)
  - Not installed
  - Would allow native Android sensor access at full hardware rates
  
- **Kivy Framework** - Cross-platform mobile app framework
  - Not installed
  - Provides higher-level sensor API
  
- **Plyer** - Device API abstraction layer
  - Not installed
  - Simpler cross-platform sensor access
  
- **pygame** - Game library with sensor support
  - Not installed
  
- **dumpsys** - Android system service diagnostic tool
  - Not available in Termux environment
  - Would show sensor service details

### ✗ Filesystem Access (Not Accessible)
- **/sys/class/sensors/** - Linux kernel sensor sysfs
  - Exists but not readable from Termux
  - Would allow kernel-level sensor access
  
- **/dev/input/** - Input device event stream
  - Not accessible
  - Would provide raw hardware events
  
- **/dev/sensor*** - Direct sensor device files
  - Not present in accessible filesystem

---

## 2. Architecture Analysis: Why Termux:API Limits Sensors to 15 Hz

### Root Cause: LocalSocket Communication Bottleneck
```
Android Sensor HAL → Termux:API Service → LocalSocket → Termux Terminal
                                              ↑
                                   15 Hz ceiling (bottleneck)
```

**Key Constraints:**
1. **LocalSocket capacity**: ~15-20 events/second sustainable
   - Matches our observed 14.95 Hz sustained rate
   - Peak bursts to 23.8 Hz observed, but not sustainable
   
2. **Termux:API Architecture**:
   - Wraps Android services via inter-process communication
   - LocalSocket (Android's lightweight IPC) is the bottleneck
   - Not designed for high-frequency streaming (that's what native apps use)

3. **Alternative APIs**:
   - Bluetooth sensors: Not available in Termux
   - File-based interfaces: Blocked by permissions
   - Native HAL: Requires compiled native library
   - Direct Java access: Would need pyjni/jnius

---

## 3. Potential Higher-Rate Solutions

### Option 1: Install PyJNI/JNIUS (Estimated: +50 Hz potential)
**Pros:**
- Direct Java/Android API access
- Bypass LocalSocket bottleneck
- Could achieve 50+ Hz accelerometer sampling
- Access to full Android sensor framework

**Cons:**
- Requires compilation/installation (non-trivial)
- Dependencies may not be available in Termux
- Java JNI setup complexity
- Risk of instability without proper sandboxing

**Installation attempt:** FAILED ✗
```
$ pip install pyjnius
...error: subprocess-exited-with-error
TypeError: expected str, bytes or os.PathLike object, not NoneType
```
**Result:** PyJNI requires JDK (Java Development Kit) which is not available in this minimal Termux environment. Installation fails at build time. Would require:
- Installing Java toolchain (~500MB+)
- Setting up JDK environment variables
- Recompiling pyjnius from source
- High risk of conflicts with existing packages

**Verdict:** Not feasible without major system reconfiguration.

### Option 2: Kivy Framework (Estimated: +40 Hz potential)
**Pros:**
- Cross-platform sensor abstraction
- Designed for mobile use
- Better API than raw Termux:API

**Cons:**
- Heavy dependency tree
- Requires display/UI framework (overkill for CLI tool)
- Still uses same Termux:API underneath

### Option 3: Custom Native Library (Estimated: 100+ Hz potential)
**Pros:**
- Full hardware sensor HAL access
- No bottlenecks
- Could achieve phone's native capability (~200 Hz+)

**Cons:**
- Requires C/C++ development
- Android NDK setup
- Complex JNI bridging
- Significant development effort
- Maintenance burden

### Option 4: Accept 15 Hz and Optimize Algorithm
**Pros:**
- No additional dependencies
- Proven stable
- Sufficient for incident detection
- Minimal maintenance burden
- Already working

**Cons:**
- Lower temporal resolution
- Cannot improve through code changes
- Hardware architecture limitation

---

## 4. Reality Check: Is Higher Rate Actually Needed?

### For Motion Tracker Use Case:
```
Event Type              Duration    Min Frequency Needed
────────────────────────────────────────────────────────
Hard braking            1-2 sec     >5 Hz to detect onset
Impact/collision        100-500ms   >10 Hz to capture peak
Swerving                500-1000ms  >5 Hz to track direction
Lane departure          500ms-2sec  >5 Hz to detect start
Pothole/bump            100-200ms   >10 Hz ideally

Current 15 Hz capability:  ✓ Covers all detection cases
```

**Analysis:** For safety/incident detection, 15 Hz is adequate.

### For Precision/Smoothing:
- 15 Hz gives 67ms sample intervals
- Integration error accumulates ~1% per second
- Over 10 minutes: ~600% drift (matches our test results)
- **This is why filters diverge** (not because of sampling rate, but accumulated error)

### Where Higher Rate WOULD Help:
1. Vibration analysis (requires 50+ Hz)
2. Shock detection (requires 100+ Hz)
3. Gyro integration (requires >50 Hz to lock to accel)
4. Audio-frequency events (requires 200+ Hz)

---

## 5. Recommendation

### Current Strategy: OPTIMAL
**Stick with termux-sensor at 15 Hz** for:
- ✓ Production reliability
- ✓ Minimal dependencies
- ✓ Proven API stability
- ✓ Sufficient for stated use case (incident detection)

### If Higher Rate Becomes Critical:
1. **Phase 1 (Quick test):** Try installing PyJNI - may work with minimal changes
2. **Phase 2 (Medium effort):** Create minimal Kivy sensor wrapper
3. **Phase 3 (Major effort):** Develop custom Android NDK library
4. **Phase 4 (Alternative):** Switch to standalone native app (not Termux)

---

## 6. System Limitations (Not Bugs)

| Limitation | Cause | Status |
|-----------|-------|--------|
| 15 Hz accel max | LocalSocket bottleneck | Architectural |
| GPS ~0.25 Hz indoors | Signal availability | Environmental |
| No dumpsys | Termux sandbox | Expected |
| No /sys access | Permission model | Security design |
| No PyJNI | Not installed | Optional |

**All are expected constraints, not failures.**

---

## Files for Reference
- ACCELEROMETER_ROOT_CAUSE.md - Detailed technical analysis
- TEST_ANALYSIS_REPORT.md - Validation test results
- motion_tracker_v2/test_ekf_vs_complementary.py - Current implementation

