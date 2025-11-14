# Investigation Answers - Health Monitor Failure

## 1. HEALTH MONITOR ACTIVITY TIMELINE

### When was the last successful daemon restart?
**~38 minutes into test** (Line ~9234 in crash log)

Last successful restart with incremented counter:
```
✓ Accelerometer daemon restarted successfully
✓ Accel restarted, resuming data collection
```
After this, all subsequent 34 restart attempts failed validation.

### What time did auto-saves start showing "0 GPS, 0 accel"?
**~39 minutes (Auto-save #194)** - First occurrence of 0 accel samples
```
Auto-save #193: 0 GPS, 35 accel samples
Auto-save #194: 0 GPS, 0 accel samples  ← First 0
Auto-save #195: 0 GPS, 0 accel samples
```

Thereafter, most auto-saves showed 0 samples with occasional brief data bursts (1-5 minute recovery attempts).

### Gap between last successful restart and first 0-sample auto-save?
**~30 seconds**

Timeline:
- [38:00] Last successful restart validation passed
- [38:30] Auto-save #194 shows 0 accel samples
- [39:00-50:00] Health monitor tried 34 more restart attempts, all failed validation

---

## 2. HEALTH MONITOR CODE CHECK

### _health_monitor_loop location and behavior

**File:** /data/data/com.termux/files/home/gojo/motion_tracker_v2/test_ekf_vs_complementary.py  
**Lines:** 889-947

**Method structure:**
```python
def _health_monitor_loop(self):
    """Monitor sensor health and auto-restart if sensors go silent or die"""
    while not self.stop_event.is_set():
        time.sleep(self.health_check_interval)  # 2 seconds
        
        # CHECK ACCELEROMETER HEALTH
        if self.accel_daemon:
            if not self.accel_daemon.is_alive():  # Process died
                # TRIGGER RESTART
                if self.restart_counts['accel'] < self.max_restart_attempts:
                    self._restart_accel_daemon()
            else:
                # Check data silence
                silence_duration = now - self.last_accel_sample_time
                if silence_duration > self.accel_silence_threshold:
                    # TRIGGER RESTART
                    if self.restart_counts['accel'] < self.max_restart_attempts:
                        self._restart_accel_daemon()
        
        # CHECK GPS HEALTH (similar pattern)
```

### Silence thresholds (from initialization at line 487-489)
```python
self.accel_silence_threshold = 5.0   # Restart if no accel for 5 seconds
self.gps_silence_threshold = 30.0    # Restart if no GPS for 30 seconds
self.health_check_interval = 2.0     # Check health every 2 seconds
```

### Max restart attempts
```python
self.max_restart_attempts = 60  # Line 474
```

The health monitor correctly checked this limit before attempting restarts:
```python
if self.restart_counts['accel'] < self.max_restart_attempts:
    if self._restart_accel_daemon():
        self.restart_counts['accel'] += 1
```

### Are there locks that could block health monitor?

**Yes, critical race condition exists:**

Lines 1347, 1485: `with self._accel_restart_lock:` and `with self._gps_restart_lock:`

These locks are held during the ENTIRE restart process (40+ seconds), which includes:
- Process cleanup (5-10 seconds)
- New daemon creation
- Cooldown sleep (12 seconds)
- Validation with 30-second timeout
- Retry with 10-second timeout

**During these 40+ seconds, the _health_monitor_loop thread must wait** because:
- Each health check iteration happens every 2 seconds
- If a restart is in progress, the lock prevents concurrent restarts
- The validation logic blocks for up to 40 seconds

This is not the PRIMARY issue (lock contention is acceptable), but it means health monitor cannot detect new failures until restart validation completes.

---

## 3. FAILURE MODE ANALYSIS

### Did health monitor hit max restart attempts?
**NO**

The counter never incremented past 8:
```
🔄 Attempting to restart accelerometer daemon (attempt 9/60)...
...later...
🔄 Attempting to restart accelerometer daemon (attempt 9/60)...  ← Still attempt 9
...never reaches attempt 10...
```

The `self.restart_counts['accel'] += 1` line (1418, 1434) only executes on successful validation. Since validation failed 87% of the time after minute 38, the counter never advanced.

### Did health monitor thread crash/stop?
**NO**

Evidence from final 20 minutes of log:
- Health monitor continued detecting failures every 1-5 minutes
- Multiple "⚠️ ACCEL DAEMON DIED" messages in final minutes
- Multiple "⚠️ ACCEL SILENT for Xs" messages throughout
- Health monitor successfully triggered 34 restart attempts (lines show "Attempting to restart...")

Health monitor thread was running and detecting problems until the very end.

### Were restart attempts made but failed silently?
**NO - Failure was VISIBLE**

Each failed restart printed explicit failure messages:
```
✗ Accel restart failed after daemon death
✗ Accel daemon unresponsive after retry
→ No data after 30.0s, retrying...
```

Failures were logged clearly. However, the underlying CAUSE (race condition with production queue) was not obvious from logs.

### Error logs showing why restarts weren't attempted?
**Restarts WERE attempted (43 total)**

No error preventing attempts. The problem was validation logic, not attempt logic.

---

## 4. ROOT CAUSE SUMMARY

**Health monitor is NOT the problem.**

Health monitor:
- ✓ Correctly detected 43 daemon failures
- ✓ Triggered restart attempts appropriately
- ✓ Used correct thresholds (5s accel silence, 30s GPS silence)
- ✓ Never hit max_restart_attempts limit (counter stuck at 8)

**The problem is restart validation logic in `_restart_accel_daemon()`:**
- Line 1402: Creates new daemon with fresh empty queue
- Line 1414: Tries to validate by consuming from same queue that _accel_loop is also consuming from
- Result: Race condition, validation loses to production consumer
- Manifest: 87% validation timeout failures

**Why failures cascaded after minute 38:**
1. Early restarts succeeded (8 total) due to queue timing margin
2. Daemon became unstable (frequent crashes)
3. Restart frequency increased (2 per min → 8 per min)
4. Race condition became deterministic under high restart rate
5. Validation consistently failed, leaving daemons in zombie state

---

## Timeline Summary

| Time | Event | Status |
|------|-------|--------|
| 0-30 min | Normal operation, 0-2 restarts | Accel data flowing |
| 30-38 min | Restarts increasing, 2-4 per min | 8 successful restarts |
| 38:00 | Last successful restart | restart_counts=8 |
| 38:30 | First "0 accel" in auto-save | Data flow failing |
| 38:30-50:00 | 34 restart attempts, 30 failed (87%) | Zombie state |
| 50:00 | Test completes | Accel dead for 12 min |

---

## Key Files for Fixing

1. **test_ekf_vs_complementary.py, line 1414** - Remove queue-based validation
2. **test_ekf_vs_complementary.py, lines 1347-1485** - Refactor _restart_accel_daemon()
3. **Consider:** Add last_sample_timestamp to daemon objects for simpler validation

---

## Conclusion

The health monitor correctly detected and attempted to recover from 43 daemon failures. However, the restart validation logic used a flawed approach (consuming from production queue) that created a race condition. Under normal testing loads (0-38 min), this race was rare. But after minute 38, when daemons became unstable and restart frequency increased, the race became deterministic, causing 87% validation failures.

The health monitor is working as designed. The daemon restart validation needs redesign.
