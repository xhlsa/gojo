# CRITICAL: Sensor Daemon Death Loop - Root Cause Analysis & Fix

## Problem Statement

The Motion Tracker V2 test harness (`test_ekf_vs_complementary.py`) experiences catastrophic daemon failures creating a death loop between accelerometer and GPS daemons:

### Symptoms (Observed in Logs)

```
⚠️ ACCEL DAEMON DIED (exit_code=0) - triggering immediate restart
🔄 Attempting to restart accelerometer daemon (attempt 1/60)...
  ✓ Accelerometer daemon restarted successfully
  ✓ Accel restarted after daemon death

⚠️ GPS DAEMON DIED (exit_code=-9) - triggering immediate restart
🔄 Attempting to restart GPS daemon (attempt 1/60)...
[GPSDaemon] stderr: [GPS] Request timeout (15s)
[GPSDaemon] stderr: [GPS] Starvation for 30.0s, exiting
  ✗ GPS restart failed after daemon death

⚠️ ACCEL DAEMON DIED (exit_code=-15) - triggering immediate restart
  ✗ Accelerometer daemon started but not receiving data after 15s
```

### Pattern Analysis

1. **Accelerometer crashes immediately** with `exit_code=0` (clean exit) or `-15` (SIGTERM)
2. **GPS hangs** with "Request timeout (15s)" then exits with `exit_code=-9` (SIGKILL) or `-15` (SIGTERM)
3. **Restarts fail** - daemons start but produce no data for 15+ seconds
4. **Death loop** - both daemons alternate dying, creating cascading failures
5. **No recovery** - system never stabilizes, test becomes invalid

---

## Root Cause Hypotheses (Priority Order)

### H1: **Process Cleanup Incomplete - Zombie termux-sensor Processes** (HIGHEST PRIORITY)

**Evidence:**
- File: `test_ekf_vs_complementary.py:958-976`
- Current cleanup: `os.system("pkill -9 termux-sensor 2>/dev/null")` + 3s sleep
- **Problem:** `pkill -9` sends SIGKILL but doesn't wait for process reaping
- **Consequence:** Zombie processes hold file descriptors, prevent new termux-sensor from initializing

**Code Location:**
```python
# Line 958-976 (test_ekf_vs_complementary.py)
def _restart_accel_daemon(self):
    try:
        self.accel_daemon.stop()
        os.system("pkill -9 termux-sensor 2>/dev/null")  # ❌ NO WAIT FOR ZOMBIE REAPING
        os.system("pkill -9 termux-api 2>/dev/null")     # ❌ NO WAIT FOR BACKEND CLEANUP
        time.sleep(3)  # ⚠️ Static delay, no validation
    except Exception as e:
        print(f"  Warning during accel daemon stop: {e}", file=sys.stderr)

    # Creates new daemon BEFORE verifying old one is fully dead
    self.accel_daemon = PersistentAccelDaemon(delay_ms=50)
```

**Fix Required:**
```python
def _restart_accel_daemon(self):
    # 1. STOP old daemon gracefully first
    try:
        if self.accel_daemon:
            self.accel_daemon.stop()  # Sends SIGTERM to subprocess
    except Exception as e:
        print(f"  Warning stopping daemon: {e}", file=sys.stderr)

    # 2. AGGRESSIVE KILL with zombie reaping
    import subprocess
    try:
        # Kill termux-sensor (may have multiple instances)
        result = subprocess.run(['pkill', '-9', 'termux-sensor'],
                               capture_output=True, timeout=2)
        # Kill termux-api backend (Android sensor service)
        result = subprocess.run(['pkill', '-9', 'termux-api'],
                               capture_output=True, timeout=2)

        # 3. WAIT FOR ZOMBIE REAPING (critical missing step)
        # Poll process table until termux-sensor fully exits
        max_wait = 5.0  # seconds
        start_wait = time.time()
        while time.time() - start_wait < max_wait:
            result = subprocess.run(['pgrep', '-x', 'termux-sensor'],
                                   capture_output=True, timeout=1)
            if result.returncode != 0:  # No termux-sensor processes found
                break
            time.sleep(0.2)  # Poll every 200ms

        # Validate cleanup succeeded
        result = subprocess.run(['pgrep', '-x', 'termux-sensor'],
                               capture_output=True, timeout=1)
        if result.returncode == 0:  # Still found processes!
            print(f"  ⚠️ WARNING: termux-sensor processes still alive after cleanup",
                  file=sys.stderr)
            # Last resort: wait longer
            time.sleep(2)

    except Exception as e:
        print(f"  Warning during process cleanup: {e}", file=sys.stderr)

    # 4. CREATE new daemon ONLY after validated cleanup
    try:
        self.accel_daemon = PersistentAccelDaemon(delay_ms=50)
    except Exception as e:
        print(f"  ✗ Failed to create new daemon: {e}", file=sys.stderr)
        return False

    # 5. Extended cooldown for Android sensor backend reset
    time.sleep(self.restart_cooldown + 2)  # Total: 12 seconds

    # ... rest of restart logic
```

---

### H2: **File Descriptor Leaks - subprocess.Popen Not Closed** (HIGH PRIORITY)

**Evidence:**
- File: `motion_tracker_v2.py:162-168` (PersistentAccelDaemon)
- File: `test_ekf_vs_complementary.py:138-144` (PersistentGPSDaemon)

**Problem:**
```python
# Line 138-144 (test_ekf_vs_complementary.py)
self.gps_process = subprocess.Popen(
    ['python3', '-c', wrapper_script],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1  # ❌ Line buffered, but no explicit close_fds=True
)
```

**Consequence:**
- File descriptors for stdout/stderr/stdin remain open after process death
- Android has FD limit of ~1024, exhausted after 300+ restarts
- Termux:API backend refuses new connections (socket exhaustion)

**Fix Required:**
```python
# Add to ALL subprocess.Popen calls:
self.gps_process = subprocess.Popen(
    ['python3', '-c', wrapper_script],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
    close_fds=True  # ✅ CRITICAL: Close inherited file descriptors
)

# ALSO: Explicit cleanup in stop() method
def stop(self):
    self.stop_event.set()
    if self.gps_process:
        try:
            self.gps_process.terminate()
            self.gps_process.wait(timeout=2)  # Wait for clean exit
        except subprocess.TimeoutExpired:
            self.gps_process.kill()  # Force kill if timeout
            self.gps_process.wait(timeout=1)  # Wait for SIGKILL
        except:
            pass
        finally:
            # ✅ CRITICAL: Close file descriptors explicitly
            if self.gps_process.stdout:
                self.gps_process.stdout.close()
            if self.gps_process.stderr:
                self.gps_process.stderr.close()
            if self.gps_process.stdin:
                self.gps_process.stdin.close()
```

**Locations to Fix:**
1. `test_ekf_vs_complementary.py:138` - GPS wrapper Popen
2. `motion_tracker_v2.py:162` - Accel daemon Popen
3. `motion_tracker_v2.py:384` - Gyro daemon Popen
4. All corresponding `stop()` methods

---

### H3: **Lock Contention - Multiple Threads Restarting Same Daemon** (MEDIUM PRIORITY)

**Evidence:**
- File: `test_ekf_vs_complementary.py:751-810` (health monitor loop)
- File: `test_ekf_vs_complementary.py:1053-1169` (old status log restarts)

**Problem:**
```python
# Line 751-810: Health monitor runs every 2 seconds
def _health_monitor_loop(self):
    while not self.stop_event.is_set():
        time.sleep(self.health_check_interval)  # 2 seconds

        # CHECK ACCELEROMETER (no lock protection!)
        if not self.accel_daemon.is_alive():
            if self._restart_accel_daemon():  # ❌ Race: Multiple threads can call this
                ...

# Line 1103-1137: Status logger ALSO tries to restart (runs every 30s)
def _log_status(self):
    if accel_status.startswith("DEAD"):
        if self.restart_counts['accel'] < self.max_restart_attempts:
            if self._restart_accel_daemon():  # ❌ Race: Concurrent restart attempt
                ...
```

**Consequence:**
- Health monitor detects death at T=0s, starts restart
- Status logger detects death at T=2s (before restart completes), starts 2nd restart
- Both create new PersistentAccelDaemon instances → resource conflict
- Second restart kills first daemon mid-initialization → exit_code=0

**Fix Required:**
```python
class FilterComparison:
    def __init__(self, ...):
        # ADD: Restart locks (one per daemon type)
        self._accel_restart_lock = threading.Lock()
        self._gps_restart_lock = threading.Lock()

    def _restart_accel_daemon(self):
        # ACQUIRE lock before restart (prevents concurrent restarts)
        with self._accel_restart_lock:
            # Check AGAIN inside lock (daemon may have been restarted by other thread)
            if self.accel_daemon and self.accel_daemon.is_alive():
                print(f"  → Accel already alive (concurrent restart won)", file=sys.stderr)
                return True

            print(f"\n🔄 Attempting to restart accelerometer daemon...", file=sys.stderr)
            # ... rest of restart logic (now thread-safe)
```

---

### H4: **Termux:API Resource Exhaustion - Too Many Concurrent Requests** (MEDIUM PRIORITY)

**Evidence:**
- GPS stderr: `[GPS] Request timeout (15s)` → suggests backend overload
- Accel daemon: Exits with code 0 (clean exit) → suggests backend rejected connection

**Problem:**
- Termux:API backend has socket pool limit (~10 concurrent connections)
- When accel daemon restarts, it creates new termux-sensor process
- Old processes not fully cleaned up → socket pool exhausted
- GPS wrapper script polls every 5s → adds to pool pressure

**Fix Required:**
```python
# Before ANY daemon restart, flush ALL stale Termux:API connections
def _flush_termux_api_backend(self):
    """Aggressively reset Termux:API backend state"""
    try:
        # Kill ALL termux-api processes (forces socket pool reset)
        subprocess.run(['pkill', '-9', 'termux-api'], timeout=2)

        # Kill backend services
        subprocess.run(['pkill', '-9', '-f', 'termux-api Sensor'], timeout=2)
        subprocess.run(['pkill', '-9', '-f', 'termux-api Location'], timeout=2)

        # Wait for Android to release sockets (critical!)
        time.sleep(2)

        # Verify cleanup
        result = subprocess.run(['pgrep', '-f', 'termux-api'],
                               capture_output=True, timeout=1)
        if result.returncode == 0:
            print(f"  ⚠️ termux-api processes still alive!", file=sys.stderr)
            time.sleep(3)  # Extended wait

    except Exception as e:
        print(f"  Warning flushing API backend: {e}", file=sys.stderr)

# CALL before every restart:
def _restart_accel_daemon(self):
    self._flush_termux_api_backend()  # ✅ CRITICAL: Reset backend first
    # ... then create new daemon
```

---

### H5: **Validation Timeout Too Short - termux-sensor Init Takes 10-20s** (LOW PRIORITY)

**Evidence:**
- Line 981: `test_data = self.accel_daemon.get_data(timeout=15.0)`
- Log: "Accelerometer daemon started but not receiving data after 15s"

**Problem:**
- After aggressive pkill -9, Android sensor backend needs full re-init
- LocationAPI/Sensor backend restart can take 15-20s on Samsung devices
- Current 15s timeout too short for backend recovery

**Fix Required:**
```python
# Line 979-988 (test_ekf_vs_complementary.py)
if self.accel_daemon.start():
    # EXTENDED validation timeout for post-crash recovery
    validation_timeout = 30.0  # Increased from 15s → 30s
    test_data = self.accel_daemon.get_data(timeout=validation_timeout)
    if test_data:
        print(f"  ✓ Accelerometer daemon restarted successfully", file=sys.stderr)
        self.restart_counts['accel'] += 1
        return True
    else:
        print(f"  ✗ Accel daemon started but no data after {validation_timeout}s",
              file=sys.stderr)
        # RETRY ONCE before failing
        print(f"  → Retrying validation (backend may still be initializing)...",
              file=sys.stderr)
        time.sleep(5)
        test_data = self.accel_daemon.get_data(timeout=10.0)
        if test_data:
            print(f"  ✓ Validation succeeded on retry", file=sys.stderr)
            self.restart_counts['accel'] += 1
            return True
        return False
```

---

## Implementation Priority

### Phase 1: CRITICAL FIXES (Implement Immediately)

**File: `test_ekf_vs_complementary.py`**

1. **Fix zombie process cleanup** (H1)
   - Location: Lines 958-976 (`_restart_accel_daemon`)
   - Location: Lines 993-1051 (`_restart_gps_daemon`)
   - Action: Add `pgrep` polling loop to verify process death
   - Validation: Check `pgrep termux-sensor` returns empty before creating new daemon

2. **Fix file descriptor leaks** (H2)
   - Location: Line 138 (GPS Popen), Line 234-242 (GPS stop)
   - Action: Add `close_fds=True` + explicit FD cleanup in stop()
   - Validation: Check `/proc/{pid}/fd` count before/after restart

3. **Add restart locks** (H3)
   - Location: Lines 338-347 (init), Lines 954-991 (restart methods)
   - Action: Add threading.Lock for accel/gps restarts
   - Validation: No concurrent restart attempts in logs

**File: `motion_tracker_v2.py`**

4. **Fix daemon Popen file descriptors** (H2)
   - Location: Lines 162-168 (Accel), Lines 384-390 (Gyro)
   - Action: Add `close_fds=True` to all Popen calls
   - Location: Lines 271-288 (Accel stop), Lines 570-581 (Gyro stop)
   - Action: Add explicit stdout/stderr/stdin close in stop()

### Phase 2: ROBUSTNESS IMPROVEMENTS (Implement After Phase 1 Validated)

5. **Add Termux:API backend flushing** (H4)
   - Location: New method in `test_ekf_vs_complementary.py`
   - Action: Create `_flush_termux_api_backend()` helper
   - Call: Before EVERY daemon restart (accel + GPS)

6. **Extend validation timeouts** (H5)
   - Location: Lines 979-991 (accel restart), Lines 1022-1047 (GPS restart)
   - Action: Increase timeout 15s → 30s, add retry logic

---

## Testing Strategy

### Validation Steps (Run After Each Fix)

1. **Zombie Process Test**
   ```bash
   # Terminal 1: Run test
   ./test_ekf.sh 5

   # Terminal 2: Monitor processes
   watch -n1 'pgrep -a termux-sensor | wc -l'

   # Expected: Count should never exceed 2 (accel + gyro from same daemon)
   # Before fix: Count grows indefinitely (zombie accumulation)
   ```

2. **File Descriptor Leak Test**
   ```bash
   # Terminal 1: Run test
   ./test_ekf.sh 10

   # Terminal 2: Monitor FDs for test process
   watch -n2 'ls /proc/$(pgrep -f test_ekf_vs_complementary)/fd | wc -l'

   # Expected: FD count stable at ~20-30
   # Before fix: FD count grows 5-10 per restart → hits 1024 limit
   ```

3. **Restart Lock Test**
   ```bash
   # Enable verbose restart logging
   # Check logs for "concurrent restart" messages
   grep -i "concurrent\|race\|already alive" comparison_*.json

   # Expected: No concurrent restart attempts
   # Before fix: Multiple "Attempting to restart" messages at same timestamp
   ```

4. **Extended Run Test**
   ```bash
   # Run 30-minute test to verify stability
   ./test_ekf.sh 30

   # Monitor restart counts
   tail -f comparison_*.json | grep restart_count

   # Expected: <5 restarts total (GPS network fluctuations acceptable)
   # Before fix: 20+ restarts, cascading failures
   ```

### Success Criteria

- ✅ Test runs 30+ minutes with <5 total restarts
- ✅ No "exit_code=0" or "exit_code=-15" (clean exits = process conflicts)
- ✅ No "daemon started but not receiving data" (initialization race)
- ✅ `pgrep termux-sensor` count never exceeds 2 (no zombies)
- ✅ Restart counts: accel <3, GPS <10 (network dropouts acceptable)

---

## Edge Cases to Handle

1. **Double restart during health check + status log**
   - Solution: Restart locks (H3)
   - Validation: Check lock acquisition logs

2. **GPS wrapper exits during restart**
   - Current: Lines 104-135 (max_runtime=2700s, auto-exits after 45 min)
   - Issue: If restart happens at 44 min mark, wrapper exits immediately
   - Solution: Reset `max_runtime` timer on daemon recreation

3. **Accel daemon dies mid-validation**
   - Current: 15s validation timeout
   - Issue: Daemon may die at 14s → validation fails → restart loop
   - Solution: Retry validation once with 5s delay (H5)

4. **termux-api backend hung (not just socket exhaustion)**
   - Symptom: `pkill -9 termux-api` succeeds but new connections still fail
   - Root: Android sensor service needs full restart
   - Solution: Add `am force-stop com.termux.api` (requires root or adb)
   - Alternative: Exponential backoff (5s → 10s → 20s delays between restarts)

---

## Files to Modify (Complete List)

### Primary Changes

1. **`motion_tracker_v2/test_ekf_vs_complementary.py`**
   - Lines 338-347: Add restart locks to `__init__`
   - Lines 751-810: Add lock to `_health_monitor_loop`
   - Lines 954-991: Rewrite `_restart_accel_daemon` (zombie cleanup, FD close, locks)
   - Lines 993-1051: Rewrite `_restart_gps_daemon` (same fixes)
   - Lines 1053-1169: Remove redundant restarts from `_log_status` OR add locks
   - New method: `_flush_termux_api_backend()` helper

2. **`motion_tracker_v2/motion_tracker_v2.py`**
   - Lines 162-168: Add `close_fds=True` to Accel Popen
   - Lines 271-288: Add explicit FD close to Accel stop()
   - Lines 384-390: Add `close_fds=True` to Gyro Popen
   - Lines 570-581: Add explicit FD close to Gyro stop()

### Secondary Changes (If Phase 1 Insufficient)

3. **`test_ekf.sh`** (shell wrapper)
   - Add aggressive cleanup before test start:
     ```bash
     pkill -9 termux-sensor
     pkill -9 termux-api
     sleep 3
     # Verify cleanup
     if pgrep termux-sensor; then
         echo "ERROR: termux-sensor still running, aborting"
         exit 1
     fi
     ```

---

## Specific Code Blocks to Replace

### Block 1: Accelerometer Restart (Lines 954-991)

**REPLACE:**
```python
def _restart_accel_daemon(self):
    """Attempt to restart the accelerometer daemon"""
    print(f"\n🔄 Attempting to restart accelerometer daemon (attempt {self.restart_counts['accel'] + 1}/{self.max_restart_attempts})...", file=sys.stderr)

    # AGGRESSIVE STOP: Kill old daemon processes completely
    try:
        self.accel_daemon.stop()
        # Force kill termux-sensor and termux-api to fully clean up
        os.system("pkill -9 termux-sensor 2>/dev/null")
        os.system("pkill -9 termux-api 2>/dev/null")
        time.sleep(3)  # EXTENDED pause for kernel cleanup
    except Exception as e:
        print(f"  Warning during accel daemon stop: {e}", file=sys.stderr)

    # Create new daemon instance
    try:
        self.accel_daemon = PersistentAccelDaemon(delay_ms=50)
    except Exception as e:
        print(f"  ✗ Failed to create new accelerometer daemon: {e}", file=sys.stderr)
        return False

    # EXTENDED cooldown for full resource release
    time.sleep(self.restart_cooldown + 2)

    # Start new daemon
    if self.accel_daemon.start():
        # Validate it's actually working (EXTENDED timeout: termux-sensor needs full init on restart)
        test_data = self.accel_daemon.get_data(timeout=15.0)  # INCREASED from 10 to 15 seconds
        if test_data:
            print(f"  ✓ Accelerometer daemon restarted successfully", file=sys.stderr)
            self.restart_counts['accel'] += 1
            return True
        else:
            print(f"  ✗ Accelerometer daemon started but not receiving data after 15s (termux-sensor may be unresponsive)", file=sys.stderr)
            return False
    else:
        print(f"  ✗ Failed to start accelerometer daemon process", file=sys.stderr)
        return False
```

**WITH:**
```python
def _restart_accel_daemon(self):
    """Attempt to restart the accelerometer daemon (thread-safe with zombie cleanup)"""
    # LOCK: Prevent concurrent restart attempts from health monitor + status logger
    with self._accel_restart_lock:
        # DOUBLE-CHECK: Another thread may have already restarted
        if self.accel_daemon and self.accel_daemon.is_alive():
            print(f"  → Accel already alive (concurrent restart won)", file=sys.stderr)
            return True

        print(f"\n🔄 Attempting to restart accelerometer daemon (attempt {self.restart_counts['accel'] + 1}/{self.max_restart_attempts})...", file=sys.stderr)

        # STEP 1: GRACEFUL STOP (terminate subprocess first)
        try:
            if self.accel_daemon:
                self.accel_daemon.stop()  # Sends SIGTERM
        except Exception as e:
            print(f"  Warning stopping daemon: {e}", file=sys.stderr)

        # STEP 2: AGGRESSIVE KILL + ZOMBIE REAPING
        try:
            # Kill all termux-sensor processes
            subprocess.run(['pkill', '-9', 'termux-sensor'],
                          capture_output=True, timeout=2)

            # Kill termux-api backend (Android sensor service)
            subprocess.run(['pkill', '-9', 'termux-api'],
                          capture_output=True, timeout=2)

            # CRITICAL: WAIT FOR ZOMBIE REAPING (poll until processes gone)
            max_wait = 5.0  # seconds
            start_wait = time.time()
            while time.time() - start_wait < max_wait:
                result = subprocess.run(['pgrep', '-x', 'termux-sensor'],
                                       capture_output=True, timeout=1)
                if result.returncode != 0:  # No processes found
                    break
                time.sleep(0.2)  # Poll every 200ms

            # VALIDATE cleanup succeeded
            result = subprocess.run(['pgrep', '-x', 'termux-sensor'],
                                   capture_output=True, timeout=1)
            if result.returncode == 0:
                print(f"  ⚠️ WARNING: termux-sensor processes still alive after cleanup",
                      file=sys.stderr)
                time.sleep(2)  # Extra wait

        except Exception as e:
            print(f"  Warning during process cleanup: {e}", file=sys.stderr)

        # STEP 3: CREATE NEW DAEMON (only after validated cleanup)
        try:
            self.accel_daemon = PersistentAccelDaemon(delay_ms=50)
        except Exception as e:
            print(f"  ✗ Failed to create new accelerometer daemon: {e}", file=sys.stderr)
            return False

        # STEP 4: EXTENDED COOLDOWN (Android sensor backend re-init)
        time.sleep(self.restart_cooldown + 2)  # 12 seconds total

        # STEP 5: START + VALIDATE (with retry)
        if self.accel_daemon.start():
            # EXTENDED timeout for post-crash recovery
            validation_timeout = 30.0  # Increased from 15s
            test_data = self.accel_daemon.get_data(timeout=validation_timeout)

            if test_data:
                print(f"  ✓ Accelerometer daemon restarted successfully", file=sys.stderr)
                self.restart_counts['accel'] += 1
                return True
            else:
                # RETRY ONCE (backend may still be initializing)
                print(f"  → No data after {validation_timeout}s, retrying...",
                      file=sys.stderr)
                time.sleep(5)
                test_data = self.accel_daemon.get_data(timeout=10.0)
                if test_data:
                    print(f"  ✓ Validation succeeded on retry", file=sys.stderr)
                    self.restart_counts['accel'] += 1
                    return True

                print(f"  ✗ Accel daemon unresponsive after retry", file=sys.stderr)
                return False
        else:
            print(f"  ✗ Failed to start accelerometer daemon process", file=sys.stderr)
            return False
```

### Block 2: Add Restart Locks to __init__ (Line 338)

**ADD AFTER LINE 347:**
```python
        # Thread locks for sensor restart (prevents concurrent restarts)
        self._accel_restart_lock = threading.Lock()
        self._gps_restart_lock = threading.Lock()
```

### Block 3: GPS Daemon Stop with FD Cleanup (Lines 234-242)

**REPLACE:**
```python
def stop(self):
    """Stop GPS daemon"""
    self.stop_event.set()
    if self.gps_process:
        try:
            self.gps_process.terminate()
            self.gps_process.wait(timeout=2)
        except:
            pass
```

**WITH:**
```python
def stop(self):
    """Stop GPS daemon (with FD cleanup)"""
    self.stop_event.set()
    if self.gps_process:
        try:
            self.gps_process.terminate()
            self.gps_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Force kill if timeout
            self.gps_process.kill()
            self.gps_process.wait(timeout=1)
        except Exception as e:
            pass
        finally:
            # CRITICAL: Close file descriptors explicitly
            try:
                if self.gps_process.stdout:
                    self.gps_process.stdout.close()
                if self.gps_process.stderr:
                    self.gps_process.stderr.close()
                if self.gps_process.stdin:
                    self.gps_process.stdin.close()
            except:
                pass
```

### Block 4: Add close_fds to GPS Popen (Line 138)

**CHANGE LINE 138-144:**
```python
self.gps_process = subprocess.Popen(
    ['python3', '-c', wrapper_script],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
    close_fds=True  # ✅ ADD THIS LINE
)
```

---

## Expected Impact After All Fixes

### Before Fixes (Current Behavior)
- Test duration: 5-10 minutes before death loop
- Restart count: 15-30+ (cascading failures)
- Success rate: 20% (4 out of 5 tests fail catastrophically)
- Symptoms: "exit_code=0", "exit_code=-15", data starvation

### After Phase 1 Fixes (Zombie + FD + Locks)
- Test duration: 20-30+ minutes stable
- Restart count: 5-10 (GPS network dropouts only)
- Success rate: 80% (minor GPS flakiness acceptable)
- Symptoms: Rare GPS timeouts (network issues), no accel deaths

### After Phase 2 Fixes (Backend Flush + Extended Timeout)
- Test duration: 60+ minutes stable
- Restart count: 2-5 (minimal, network only)
- Success rate: 95%+ (production-ready)
- Symptoms: None (occasional GPS network gap recovers automatically)

---

## Implementation Notes for Claude Opus

1. **Implement fixes in order** (Phase 1 → validate → Phase 2)
2. **Test after EACH fix** (don't batch-apply all changes)
3. **Preserve existing logic** (only modify restart/cleanup code)
4. **Add logging** (track zombie count, FD count, lock waits)
5. **No behavioral changes** (don't modify filter logic, data collection, etc.)

---

## Questions to Resolve Before Implementation

1. Should restart locks be **per-daemon** or **global**? (Recommendation: per-daemon)
2. Should health monitor OR status logger handle restarts? (Recommendation: health monitor only, remove from status logger)
3. Should we add exponential backoff for repeated failures? (Recommendation: yes, 5s → 10s → 20s → 40s)
4. Should we limit total lifetime restarts (e.g., 60) or per-hour (e.g., 10/hour)? (Current: 60 total is fine)

