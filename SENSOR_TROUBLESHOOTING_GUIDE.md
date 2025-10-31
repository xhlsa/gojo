# Sensor Troubleshooting Quick Reference

## Common Issues & Solutions

### Issue 1: "No accelerometer data received after 10 seconds"

**Cause:** Zombie `termux-api Sensor` process holding sensor lock

**Quick fix:**
```bash
pkill -9 -f "termux-api.*Sensor"
sleep 5
./test_ekf.sh 10  # Retry
```

**If that fails:**
```bash
# Full nuclear option
pkill -9 -f termux-sensor
pkill -9 -f termux-api
pkill -9 -f test_ekf
sleep 5
./test_ekf.sh 10
```

### Issue 2: Test starts but "Accel samples: 0"

**Cause:** Sensor daemon running but not producing data

**Check sensor manually:**
```bash
termux-sensor -s ACCELEROMETER -d 50 -n 2
```

**If output is empty or errors:**
1. Restart Termux app completely
2. Check phone settings → sensor permissions
3. Try other sensor apps to verify hardware works

### Issue 3: Script hangs during cleanup

**Cause:** Zombie processes not responding to signals

**Force cleanup:**
```bash
# Kill all Termux processes (CAREFUL!)
pkill -9 -u $(id -u) -f termux

# Then restart and try again
./test_ekf.sh 10
```

### Issue 4: "Permission denied" errors

**Cause:** Termux API permissions not granted

**Fix:**
1. Install/reinstall Termux:API app from F-Droid
2. Grant sensor permissions in Android settings
3. Run: `termux-sensor -l` to list available sensors

### Issue 5: Python test fails but manual sensor works

**Cause:** Using direct Python instead of shell script

**Wrong:**
```bash
python motion_tracker_v2/test_ekf_vs_complementary.py 10
```

**Correct:**
```bash
./test_ekf.sh 10
```

## Diagnostic Commands

### Check for zombie processes
```bash
ps aux | grep -E "termux-api.*Sensor|termux-sensor" | grep -v grep
```

### Test accelerometer access
```bash
timeout 5 termux-sensor -s ACCELEROMETER -d 50 -n 2
```

### List available sensors
```bash
termux-sensor -l
```

### Check recent test results
```bash
ls -lht comparison_*.json | head -5
```

### Monitor sensor stream live
```bash
termux-sensor -s ACCELEROMETER -d 50
# Ctrl+C to stop
```

## Known Good States

### Successful test initialization
```
Sensor initialization attempt 1/3
✓ Sensor cleanup complete
✓ Accelerometer responding correctly
✓ Sensor ready, starting test...
✓ Accelerometer responding with data on attempt 2
```

### Successful test completion
```
GPS fixes: 120+ | Accel samples: 30000+
✓ Results saved to: comparison_YYYY-MM-DD_HH-MM-SS.json
```

### Clean process state (no zombies)
```bash
$ ps aux | grep termux-api | grep -v grep
# Should return nothing (empty output)
```

## When to Restart Termux

**Symptoms requiring restart:**
- All retry attempts fail
- Manual sensor test shows no output
- `pkill -9` doesn't clean up processes
- Permission errors persist after fixes

**How to restart:**
1. Close Termux completely (swipe away from recents)
2. Force stop in Android settings (optional)
3. Wait 5 seconds
4. Reopen Termux
5. `cd ~/gojo && ./test_ekf.sh 10`

## Preventive Measures

### Always use shell script
```bash
# Good - uses robust initialization
./test_ekf.sh 10

# Bad - bypasses cleanup/validation
python motion_tracker_v2/test_ekf_vs_complementary.py 10
```

### Check for zombies before starting
```bash
# Quick check
ps aux | grep -E "termux-api.*Sensor" | grep -v grep

# If any found, clean first
pkill -9 -f "termux-api.*Sensor" && sleep 5
```

### Don't interrupt during sensor initialization
Wait for "✓ Sensor ready, starting test..." before considering Ctrl+C

## Architecture Reference

### Process hierarchy
```
test_ekf.sh (shell)
    ├── cleanup_sensors() - Kills zombies
    ├── validate_sensor() - Pre-flight check
    └── Python test
        └── termux-sensor
            └── termux-api Sensor (BACKEND - must kill explicitly)
```

### Critical kill patterns
```bash
pkill -9 -f "termux-sensor"              # Wrapper
pkill -9 -f "termux-api.*Sensor"         # Backend (CRITICAL)
pkill -9 -f "termux-api-broadcast.*Sensor"  # Alternate backend name
```

## Quick Test Validation

### Minimal smoke test (6 seconds)
```bash
./test_ekf.sh 0.1
# Should show:
# - Sensor initialization success
# - Accelerometer data flowing
# - GPS fixes: 1-2, Accel samples: 100+
```

### Full test (10 minutes)
```bash
./test_ekf.sh 10
# Should show:
# - GPS fixes: 120+
# - Accel samples: 30000+
# - Results saved successfully
```

## Error Message Meanings

| Error | Meaning | Fix |
|-------|---------|-----|
| No accelerometer data after 10 seconds | Zombie process or sensor locked | `pkill -9 -f "termux-api.*Sensor" && sleep 5` |
| Accel samples: 0 | Sensor not producing data | Check manual: `termux-sensor -s ACCELEROMETER` |
| Permission denied | Termux API not granted sensor access | Grant permissions in Android settings |
| Sensor validation failed | Sensor unavailable or locked | Wait 5 seconds and retry |
| Failed after 3 attempts | Persistent sensor issue | Restart Termux app |

## Contact Points for Issues

**Documentation:**
- Full technical details: `SENSOR_INITIALIZATION_FIX.md`
- Executive summary: `ZOMBIE_PROCESS_SOLUTION_SUMMARY.md`
- This guide: `SENSOR_TROUBLESHOOTING_GUIDE.md`

**Testing:**
- Main script: `./test_ekf.sh`
- Python test: `motion_tracker_v2/test_ekf_vs_complementary.py`
- Results: `comparison_*.json`
