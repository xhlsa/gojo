# Zombie Process Prevention Improvements

## Summary
Enhanced process cleanup and daemon lifecycle management to prevent zombie processes and ensure graceful shutdown.

## Changes Made

### 1. Improved Shell Script (`motion_tracker_v2.sh`)
**Problem:** Original script didn't forward signals to Python process, making it hard to cleanly stop.
**Solution:** Added proper signal forwarding and PID tracking:
```bash
# Launch Python process and capture PID
python motion_tracker_v2/motion_tracker_v2.py "$@" &
TRACKER_PID=$!

# Forward SIGINT and SIGTERM signals
trap "kill -TERM $TRACKER_PID 2>/dev/null || true; wait $TRACKER_PID 2>/dev/null || true" SIGINT SIGTERM

# Wait for process and return exit code
wait $TRACKER_PID
exit_code=$?
exit $exit_code
```
**Benefits:**
- Ctrl+C now properly sends SIGTERM to Python process
- Script waits for process to exit before terminating
- Exit codes are properly propagated

### 2. Added `__del__` Method to PersistentAccelDaemon (lines 403-408)
**Problem:** If daemon object was garbage collected without explicit stop() call, subprocess could orphan.
**Solution:** Added destructor:
```python
def __del__(self):
    """Ensure cleanup if daemon is garbage collected without explicit stop()"""
    try:
        self.stop()
    except:
        pass  # Silently ignore errors during cleanup
```
**Benefits:**
- Daemon cleanup guaranteed even if stop() isn't explicitly called
- Python garbage collection triggers cleanup automatically
- Safety net for unexpected shutdown paths

### 3. Enhanced Thread Cleanup in MotionTrackerV2 (lines 1205-1244)
**Problem:** Original code didn't check if threads were actually alive before joining.
**Solution:** Improved cleanup with better logging and error handling:
```python
# Wait for threads with timeout
threads_to_stop = []
if self.gps_thread and self.gps_thread.is_alive():
    threads_to_stop.append(('GPS', self.gps_thread))
if self.accel_thread and self.accel_thread.is_alive():
    threads_to_stop.append(('Accel', self.accel_thread))

for name, thread in threads_to_stop:
    try:
        thread.join(timeout=2)
        if thread.is_alive():
            print(f"⚠ {name} thread did not exit cleanly (still running)")
        else:
            print(f"  ✓ {name} thread stopped")
    except Exception as e:
        print(f"⚠ Error stopping {name} thread: {e}")
```
**Benefits:**
- Checks thread.is_alive() before joining (avoids joining already-dead threads)
- Reports which threads failed to exit cleanly
- Better error messages for debugging
- Won't accidentally wait on dead threads

### 4. Improved Daemon Stop Method Call
Added exception handling and logging around sensor_daemon.stop():
```python
if self.sensor_daemon:
    try:
        self.sensor_daemon.stop()
        print("  ✓ Accelerometer daemon stopped")
    except Exception as e:
        print(f"⚠ Error stopping accelerometer daemon: {e}")
```
**Benefits:**
- Errors in daemon cleanup don't crash main cleanup
- User gets feedback on daemon shutdown status

### 5. Enhanced Process Termination Commands
Suppressed pkill output and made it more explicit:
```python
# Kill any lingering termux-sensor and stdbuf processes
# This is a safety net in case threads didn't exit cleanly
try:
    subprocess.run(['pkill', '-9', 'termux-sensor'], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['pkill', '-9', 'stdbuf'], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except:
    pass
```
**Benefits:**
- Suppresses noise from pkill output (it doesn't find processes, no need to tell user)
- Still uses SIGKILL (-9) as final safety net
- Clear code comment explains this is a safety net

## Process Cleanup Flow

### Normal Shutdown
1. User presses Ctrl+C
2. Shell script's trap catches SIGINT/SIGTERM
3. Script sends SIGTERM to Python process
4. Python signal handler sets shutdown_requested flag
5. Main loop exits
6. MotionTrackerV2.track() runs cleanup:
   - Sets stop_event
   - Joins GPS thread (with is_alive() check)
   - Joins Accel thread (with is_alive() check)
   - Calls sensor_daemon.stop()
   - pkill -9 as final safety net
7. Process exits cleanly

### Daemon Cleanup Layers
1. **Reader Thread finally block** - Always executes, terminates subprocess
2. **daemon.stop()** - Explicit stop with terminate() → wait() → kill()
3. **daemon.__del__()** - Garbage collection safety net
4. **pkill -9** - Final safety net for any stragglers

## Testing

To verify no zombie processes:
```bash
# Run tracker
python motion_tracker_v2/motion_tracker_v2.py 0.5

# In another terminal, check for zombies
ps aux | grep " Z "  # Should be empty
pgrep termux-sensor   # Should be empty after exit
```

## Technical Details

### Why Multiple Cleanup Layers?
- **Layer 1 (finally block)**: Catches exceptions in reader thread
- **Layer 2 (stop() method)**: Explicit lifecycle management
- **Layer 3 (__del__)**: Garbage collection backup
- **Layer 4 (pkill -9)**: Ultimate safety net for threading edge cases

### Signal Forwarding in Shell
The trap handler ensures:
- Python process receives signals directly
- Parent shell doesn't suppress signals
- Process exit code is preserved
- Child process reaping happens (no zombie parent)

### Thread Safety
- All thread operations check is_alive() first
- Timeout prevents indefinite waiting
- Error handling prevents cascade failures
- Logging shows cleanup status

## Potential Future Improvements

1. **Add signal handling timeout**: Kill threads if cleanup takes too long
2. **Process group management**: Use setpgrp() to ensure child process termination
3. **Health check on exit**: Verify no orphaned processes remain
4. **Cleanup registry**: Track all spawned subprocesses for guaranteed cleanup

## Files Modified

- `motion_tracker_v2.sh` - Signal forwarding, PID tracking
- `motion_tracker_v2/motion_tracker_v2.py` - __del__ method, enhanced thread cleanup, improved error reporting
