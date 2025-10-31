# Test Session Analysis - Oct 31

## Summary
Two tests run with crash logging enabled:
- **Test 1 (2 min):** ✓ SUCCESS - Accel daemon healthy
- **Test 2 (20 min):** ✓ Completed but ACCEL DAEMON DIED

## Key Finding: Accel Daemon Lifespan Issue

### Test 2 Timeline
- **00:00** - Test started, accel daemon initialized
- **12:05** - Accel daemon DIED
  - Last sample count: 1224 samples
  - Daemon process exited silently (no error message)
  - Test continued on GPS only for remaining 8 minutes

### Pattern
- Short tests (≤2 min): Accel daemon survives
- Long tests (>10 min): Accel daemon dies around 12-15 minute mark
- **Not** signal 9 (SIGKILL) - no uncontrolled Termux crash
- **Is** daemon process exiting/hanging silently

## Data Files

### Test 1 (2 min success)
- Log: `crash_logs/test_ekf_2025-10-31_13-29-30.log`
- Data: `motion_tracker_sessions/comparison_20251031_132943.json`
- Status: Valid (1254 accel + 16 GPS samples)

### Test 2 (20 min / accel dies)
- Log: `crash_logs/test_ekf_2025-10-31_13-32-34.log`
- Data: `motion_tracker_sessions/comparison_20251031_133259.json` (truncated)
- Status: **Accel daemon crashed at 12:05 mark** - last 1224 accel samples preserved

## Next Investigation

1. Check accel daemon subprocess management in `test_ekf_vs_complementary.py`
2. Look for resource leaks (file handles, memory)
3. Check if termux-sensor process is being killed/restarted
4. Review PersistentAccelDaemon code for blocking operations

## Commands to Analyze

```bash
# View crash logs with full context
./show_crashes.sh

# Check accel daemon implementation
grep -n "PersistentAccelDaemon" motion_tracker_v2/test_ekf_vs_complementary.py

# Check daemon subprocess handling
grep -n "self.daemon" motion_tracker_v2/motion_tracker_v2.py | head -20
```

