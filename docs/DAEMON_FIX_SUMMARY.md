# Motion Tracker V2 - Process 9 Termination Fix

## Diagnosis
The v2 tracker was crashing with process 9 termination due to **excessive subprocess spawning**:
- `AccelerometerThread.read_raw()` spawned a new `termux-sensor` process for **every single sample**
- At 50Hz sampling rate = **50 new processes per second**
- This overwhelmed the Termux sensor API daemon (process ~9)
- OS terminated the daemon to protect system stability

## Root Cause
```python
# OLD: Spawning subprocess 50 times per second
def read_raw(self):
    result = subprocess.run(
        ['termux-sensor', '-s', 'accelerometer', '-n', '1'],  # NEW PROCESS
        ...
    )
```

## Solution: Single Long-Lived Sensor Daemon

### New `SensorDaemon` Class
- Spawns **ONE** long-lived `termux-sensor` process with continuous streaming
- Reads continuous JSON output from a single process
- Parses multi-line JSON objects properly
- Feeds data to a queue at the desired sampling rate

### Key Implementation Details
1. **Single Process**: `termux-sensor -s accelerometer -d <delay_ms>` runs continuously
2. **Streaming Parser**: Multi-line JSON buffering with brace counting
3. **Non-blocking Queue**: Skips samples if queue is full (prevents backpressure)
4. **Graceful Shutdown**: Properly terminates daemon on exit

### Performance Impact
**Before**: 50 new processes/second → Daemon overload → Process 9 termination
**After**: 1 persistent process → Clean streaming → No crashes

### Test Results
```
✓ Calibrated accelerometer successfully
✓ GPS locked and tracking
✓ Collected 1952 accelerometer samples in 43 seconds (~45Hz)
✓ No stream errors
✓ No process termination
✓ Graceful shutdown
```

## Code Changes
1. **Added SensorDaemon class**: 100+ lines for daemon management
2. **Simplified AccelerometerThread**: Now reads from daemon queue instead of spawning processes
3. **Updated MotionTrackerV2.start_threads()**: Creates and starts daemon
4. **Updated shutdown sequence**: Properly stops daemon

## Benefits
- ✓ Eliminates subprocess spawning cascade
- ✓ Reduces CPU/memory overhead
- ✓ More stable sensor API interaction
- ✓ Better response times (no subprocess startup latency)
- ✓ No more process 9 terminations

## Testing
- Tested 1-minute duration with 50Hz sampling
- Verified accelerometer calibration works
- Confirmed GPS integration still functional
- Validated data collection and auto-save features
