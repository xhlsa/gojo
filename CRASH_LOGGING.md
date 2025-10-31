# Crash Logging & Session Tracking

## Overview
We've implemented comprehensive crash logging to prevent losing context when tests fail. Every test run now:
- Logs all output to a timestamped file
- Captures exit codes and signals (like signal 9)
- Records the exact command and arguments
- Preserves the last 50 lines of output for debugging
- Automatically shows crash info when test exits with error

## Quick Commands

### View recent crashes
```bash
./show_crashes.sh          # Shows summary of recent crashes + most recent details
```

### View all crash logs
```bash
ls -1t crash_logs/         # List all crash logs (newest first)
tail -100 crash_logs/test_ekf_*.log  # Show last 100 lines of most recent crash
```

### Run a test WITH crash logging (automatic)
```bash
./test_ekf.sh 5            # All output captured automatically
```

## Crash Log Location
- All logs: `crash_logs/test_ekf_YYYY-MM-DD_HH-MM-SS.log`
- Each test creates ONE log file with full context

## What Gets Logged

### Session Header
- Test script and arguments
- Start timestamp
- Log file path

### Full Test Output
- Sensor initialization steps
- GPS validation
- Python test output
- Any errors or warnings

### Crash Details (if test fails)
- Exit code
- Signal name (SIGKILL=9, SIGTERM=15, etc.)
- Test command
- Timestamp
- Last 50 lines of output

## Example Crash Analysis

When a test crashes with signal 9:
```
./test_ekf.sh 5

# ... test runs ...

✗ TEST CRASHED - Exit code 137
  Signal: SIGKILL (9)
  Log: crash_logs/test_ekf_2025-10-31_13-45-22.log
  View all crashes: ./show_crashes.sh
```

Then view the crash:
```bash
./show_crashes.sh
```

Shows:
```
File: crash_logs/test_ekf_2025-10-31_13-45-22.log
-------
Exit code: 137
Signal: SIGKILL (9)
Test: test_ekf.sh 5
Timestamp: 2025-10-31 13:45:22

Last 30 lines of test output:
[...shows what was happening right before crash...]
```

## How to Debug

1. **Run test with logging:**
   ```bash
   ./test_ekf.sh 5
   ```

2. **Check what crashed:**
   ```bash
   ./show_crashes.sh
   ```

3. **Review full log:**
   ```bash
   tail -200 crash_logs/test_ekf_2025-10-31_*.log
   ```

4. **Pattern detection:**
   - Does it crash at same runtime? Check log timestamps
   - Does it crash at same step? Check output context
   - Does it crash immediately or after running? See output

## Implementation Details

### Files Created
- `crash_logger.py` - Python crash logging module (for future integration)
- `test_ekf.sh` - Updated with logging hooks
- `show_crashes.sh` - Quick crash viewer script
- `crash_logs/` - Directory for all log files (auto-created)

### Log File Format
```
==============================================================================
EKF vs Complementary Filter Test - Robust Sensor Initialization
Session started: 2025-10-31 13:45:20 UTC
Test arguments: 5
Log file: crash_logs/test_ekf_2025-10-31_13-45-20.log
==============================================================================
[cleanup_sensors] Starting
✓ Sensor cleanup complete
[cleanup_sensors] Complete
Validating accelerometer access...
✓ Accelerometer responding correctly
... (full test output)
```

### Crash Log Suffix
```
===================================================================
CRASH/ERROR DETECTED
===================================================================
Exit code: 137
Signal: SIGKILL (9)
Test: test_ekf.sh 5
Timestamp: 2025-10-31 13:45:22 UTC
Log file: crash_logs/test_ekf_2025-10-31_13-45-20.log

LAST 50 LINES OF OUTPUT:
[... context before crash ...]
===================================================================
```

## Next Steps

Now you can:
1. Run test with confidence that all context is captured
2. Use `./show_crashes.sh` to analyze crashes
3. Share crash logs with debugging - they have full context
4. Identify patterns (crashes at same point, same signal, etc.)

**Ready to debug the signal 9 crash!**
The enhanced test_ekf.sh will capture what's happening right before it crashes.
