# Health Monitor Bug Summary

## The Problem (in 30 seconds)

Health monitor detected daemon failures correctly but **87% of restart attempts failed** because the restart validation logic has a race condition.

**What happened:**
- Restart code creates a NEW daemon with a fresh empty queue
- Production code (`_accel_loop`) immediately starts consuming from this queue  
- Validation code tries to consume from same queue to verify restart worked
- Both threads are competing for the same queue items
- Production code's tight polling loop (0.1s timeout) beats validation's 30s timeout
- Validation never gets any items, times out, reports "failed"
- But the daemon IS working - data IS flowing through the queue!

## The Evidence

From the crash log around minute 47:

```
🔄 Attempting to restart accelerometer daemon (attempt 9/60)...
✓ Accelerometer daemon started
✓ Auto-saved: 235 accel samples  ← DATA IS FLOWING!
→ No data after 30.0s, retrying...  ← Validation timed out
✗ Accel restart failed after daemon death  ← But marked as FAILED!
```

The daemon successfully started and data is flowing (235 samples saved), but validation failed trying to pull from the queue because `_accel_loop` was draining it.

## Why It Got Worse Over Time

- **0-38 min:** Lower failure rate, queue had enough buildup that validation could grab items
- **38-50 min:** Daemon became more unstable (dying every 1-5 min), restart attempts increased from 2 to 8+ per minute, race condition became consistent (87% failure rate)

## The Fix

Replace queue-based validation with simpler process health check. Instead of:
```python
test_data = self.accel_daemon.get_data(timeout=30.0)  # Race condition
```

Use:
```python
# Just check if process is alive and has recent data in the main loop
if self.accel_daemon.sensor_process.poll() == None and time.time() - self.last_accel_sample_time < 2.0:
    return True
```

Or duplicate the queue feed so validation has its own queue that production doesn't consume from.

## Impact

- Restart counter stuck at 8 attempts (never incremented past 9 due to cascade of failed validations)
- Accel dead for final 12 minutes (zombie state: process alive, no data flowing)
- Final test: 45k accel samples instead of 150k (30% of expected)
- GPS unaffected (no similar validation logic, stayed alive throughout)

## Files to Fix

**test_ekf_vs_complementary.py:**
- Line 1414: `test_data = self.accel_daemon.get_data(timeout=30.0)` ← Replace this validation
- Consider: Add `last_sample_timestamp` field to daemon, check it instead of consuming queue
