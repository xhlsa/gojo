# Health Monitor Investigation - Complete Analysis Index

## Quick Answer (TL;DR)

**Why health monitor failed to restart daemons in final 20 minutes:**

Health monitor detected failures correctly (43 times) but restart validation failed 87% of the time due to a queue race condition. The validation code tried to consume from the same queue that the production data loop was already consuming from, and the production loop won every race.

**Result:** Accelerometer was in "zombie state" (process running, no data flowing) for the final 12 minutes.

---

## Documents Created

### 1. **INVESTIGATION_ANSWERS.md** - Direct Answers to Your Questions
- Health monitor activity timeline (when restarts succeeded/failed)
- Silence thresholds and code locations
- Max restart limits analysis
- Root cause analysis

**Use this when you need:** Factual timeline and code references

### 2. **HEALTH_MONITOR_BUG_SUMMARY.md** - 30-Second Overview
- The problem in one paragraph
- Key evidence from logs
- Why it got worse over time
- Quick fix options

**Use this when you need:** To explain the issue to others quickly

### 3. **QUEUE_RACE_CONDITION_EXPLAINED.md** - Deep Dive Explanation
- Visual diagram of the race condition
- Timeline showing how threads compete
- Mathematical proof of why production wins
- Three fix options explained

**Use this when you need:** To understand the mechanics deeply

### 4. **HEALTH_MONITOR_FAILURE_ANALYSIS.md** - Comprehensive Report
- Full timeline with phase analysis
- Detailed root cause explanation
- Proof from log evidence
- Design flaws and alternatives
- Recommendations for fixing

**Use this when you need:** The complete picture for a code review or design discussion

---

## Key Findings Summary

### Health Monitor Status: WORKING CORRECTLY ✓
- Detected 43 daemon failures
- Triggered restart attempts appropriately
- Used correct thresholds (5s accel silence)
- Never hit max_restart_attempts limit

### Restart Validation Status: BROKEN ✗
- Failed 87% of attempts after minute 38
- Root cause: Queue race condition with production consumer
- Symptom: Validation times out even though daemon is alive and data is flowing
- Impact: Accelerometer dead for final 12 minutes (45.4k vs 150k expected samples)

### Timeline
- 0-38 min: 8 successful restarts, system recovering
- 38-50 min: 34 restart attempts, 30 failed (87% failure rate)
- 38:30: First "0 accel samples" in auto-save
- Final 12 min: Zombie state (process alive, no data)

---

## What Happened (Executive Summary)

**Setup:**
- Restart creates new daemon with fresh empty queue
- Two threads try to consume from same queue:
  1. Production code (_accel_loop): tight 0.1s timeout loop, runs ~10x/sec
  2. Validation code: 30s timeout, runs once per restart attempt

**The Race:**
- Production code's tight polling loop wins every race
- Validation timeout expires without getting any samples
- Result: Restart marked as "failed" even though daemon is alive and producing data

**Why It Got Worse:**
- Early test (0-38 min): 1-2 restarts per 10 min, queue race was rare
- Late test (38-50 min): 8-10 restarts per 10 min, queue race became deterministic
- Cascade: Each failed restart triggered another daemon crash, which triggered another restart attempt

---

## The Numbers

| Metric | Value | Source |
|--------|-------|--------|
| Total restart attempts | 43 | Log search |
| Failed validations | 30 | 87% of attempts after min 38 |
| Successful restarts | 8 | Never incremented past attempt 9 |
| Time of last successful restart | 38 min | Auto-save #194 |
| Time of first 0-sample auto-save | 38:30 | Auto-save #194 |
| Final accel sample count | 45,396 | Expected ~150,000 |
| Final memo state | 0 for last 12 min | Zombie state |

---

## Code Locations

**Main health monitor:**
- File: test_ekf_vs_complementary.py
- Lines: 889-947 (_health_monitor_loop)
- Status: Working correctly

**Broken restart validation:**
- File: test_ekf_vs_complementary.py
- Lines: 1344-1447 (_restart_accel_daemon)
- Problem: Line 1414 - queue-based validation
- Issue: Race condition with _accel_loop consuming from same queue

**Production consumer:**
- File: test_ekf_vs_complementary.py
- Lines: 825-860 (_accel_loop)
- Issue: Tight 0.1s timeout loop wins all queue races

---

## How to Fix

### Recommended: Separate Validation Queue
Create two queues in daemon:
1. `data_queue` for production (_accel_loop)
2. `validation_queue` for restart checks

Daemon pushes to BOTH, each consumer pulls from their own, no race condition.

### Alternative 1: Skip Queue Validation
Don't try to validate by consuming. Just check:
```python
if self.accel_daemon.sensor_process.poll() == None:
    return True  # Process alive is enough!
```

### Alternative 2: Timestamp-Based
Add `last_sample_timestamp` to daemon, check it instead of consuming:
```python
if time.time() - daemon.last_sample_timestamp < 2.0:
    return True  # Data is recent
```

---

## What This Teaches Us

1. **Single queue + multiple consumers = race condition**
   - Especially when consumers have different duty cycles
   - Fast producer wins under load

2. **Validation logic shouldn't starve production**
   - Validation's job is to verify, not consume
   - Consider observation-based validation instead

3. **System behavior changes under stress**
   - Design flaw was latent for 38 minutes
   - Only became obvious under high failure rate (minute 38+)
   - This is why stress testing is critical

4. **"Process alive" ≠ "Process working"**
   - Just because process exists doesn't mean it produces data
   - But consuming from queue for validation is too invasive
   - Better: separate queue or timestamp-based checks

---

## Document Usage Guide

### For Quick Debugging
1. Read: **HEALTH_MONITOR_BUG_SUMMARY.md**
2. Check: **INVESTIGATION_ANSWERS.md** section 1 (timeline)

### For Code Review
1. Read: **QUEUE_RACE_CONDITION_EXPLAINED.md** (understand the issue)
2. Reference: **HEALTH_MONITOR_FAILURE_ANALYSIS.md** (proof and context)
3. Review: **INVESTIGATION_ANSWERS.md** section 2 (code details)

### For Implementation
1. Study: **QUEUE_RACE_CONDITION_EXPLAINED.md** (fix options)
2. Reference: **HEALTH_MONITOR_FAILURE_ANALYSIS.md** (recommendations)

### For Presentation to Others
- Use: **HEALTH_MONITOR_BUG_SUMMARY.md** (slides/overview)
- Reference: **QUEUE_RACE_CONDITION_EXPLAINED.md** (diagrams/details)
- Cite: **INVESTIGATION_ANSWERS.md** (numbers/facts)

---

## Related Issues

This investigation revealed related design concerns:
1. Status display shows process state, not data flow (zombie process appears ALIVE)
2. GPS daemon had no similar queue-based validation (why it stayed alive)
3. Lock contention during restarts (40+ second lock holds)
4. No separate validation vs production queues (architectural issue)

These should be addressed in next iteration.

---

## Files Modified During This Investigation

None. This is a pure analysis of existing code and logs. The investigation identified the bug but did not attempt to fix it (awaiting your direction).

---

## Next Steps

1. Choose fix approach (recommend: separate validation queue)
2. Implement fix in _restart_accel_daemon()
3. Add unit test for queue race condition (stress test)
4. Run 50+ minute test to validate fix
5. Monitor restart success rate (should be ~100%, not 13%)

---

## Summary

The health monitor is not the problem. The daemon restart validation logic has a fundamental queue race condition that only becomes visible under high restart rates. Fix the validation approach and the health monitor will be able to recover from daemon failures throughout the full test duration.
