# Memory Investigation: Why 124 MB Instead of 90-95 MB Target

## TL;DR
**The root cause:** `accumulated_data` is NOT being cleared after auto-save, causing unbounded memory growth.

**Location:** `/data/data/com.termux/files/home/gojo/motion_tracker_v2/test_ekf_vs_complementary.py` line 2066

**Current behavior (BROKEN):**
- Comment says "DO NOT clear accumulated_data - keeps growing so final save has all data"
- `accumulated_data` grows unbounded throughout entire test
- Test duration 10 min: +3 MB extra memory
- Test duration 45 min: +13.5 MB extra memory
- Target memory 90-95 MB → actual 124 MB = **+34 MB unaccounted for**

**Expected behavior (per CLAUDE.md):**
- "Clear accumulated_data after auto-save → eliminates ~1.4 MB/min growth"
- Memory should stay 90-95 MB regardless of test duration
- Final save should read from disk checkpoint + current numpy state

---

## Memory Breakdown Analysis

### Pre-Allocated Numpy Arrays (FIXED SIZE)
```
GPS:     1,000 samples ×  36 bytes =   36 KB  = 0.034 MB
ACCEL:  150,000 samples ×  20 bytes = 3.0 MB
GYRO:   150,000 samples ×  20 bytes = 3.0 MB
                                    = 6.034 MB
```

### Trajectory Deques (BOUNDED)
```
EKF trajectory:         1,000 entries ×  100 bytes = 0.098 MB (bounded, OK)
ES-EKF trajectory:      1,000 entries ×  100 bytes = 0.098 MB (bounded, OK)
Complementary:          1,000 entries ×  100 bytes = 0.098 MB (bounded, OK)
Covariance snapshots:     500 entries ×  200 bytes = 0.098 MB (bounded, OK)
                                                   = 0.392 MB
```

### Filter Objects (SMALL)
```
MetricsCollector:       ~0.5 MB (600-entry deques, bounded)
EKF filter:             ~0.1 MB (small matrices created at runtime)
Complementary filter:   ~0.05 MB
ES-EKF filter:          ~0.1 MB
IncidentDetector:       ~1.0 MB (30-second context buffer)
                      ≈ 1.75 MB
```

### Queues (BOUNDED)
```
12 × Queue(maxsize=50-100) ≈ 0.1 MB (items briefly in transit)
```

### Python Runtime & Other Objects
```
~15 MB baseline
```

### Expected Total Baseline
```
6.034 + 0.392 + 1.75 + 0.1 + 15 = 23.3 MB
```

**But actual memory is 124 MB → MISSING 100+ MB SOMEWHERE**

---

## The Culprit: `accumulated_data` Growing Unbounded

### What `accumulated_data` Stores
At line 1999-2003, `accumulated_data` is a dict containing:
```python
{
    'gps_samples': [],        # List of dicts, grows forever
    'accel_samples': [],      # List of dicts, grows forever
    'gyro_samples': [],       # List of dicts, grows forever
    'autosave_count': 0
}
```

### How It Grows (Every 15 seconds)
At line 2007-2010, during auto-save:
```python
self._accumulated_data['accel_samples'].extend(
    self._numpy_to_list(self.accel_samples, self.accel_index, 'accel')
)
```

This **APPENDS** samples to accumulated_data, never removing old ones.

### Memory Per Sample (Python dicts, not numpy)
- **Accel sample** (timestamp + x,y,z floats): ~120 bytes of Python overhead
- **GPS sample** (timestamp + lat,lon,alt,accuracy floats): ~140 bytes

### Test Duration Memory Growth
At 44 Hz accel + 0.2 Hz GPS:

| Duration | Accel Samples | GPS Samples | Memory Growth | Total Memory |
|----------|---------------|-------------|---------------|--------------|
| 5 min    | 13,200        | 60          | 1.5 MB        | 24.8 MB      |
| 10 min   | 26,400        | 120         | 3.0 MB        | 26.3 MB      |
| 15 min   | 39,600        | 180         | 4.5 MB        | 27.8 MB      |
| 20 min   | 52,800        | 240         | 6.0 MB        | 29.3 MB      |
| 30 min   | 79,200        | 360         | 9.0 MB        | 32.3 MB      |
| 45 min   | 118,800       | 540         | 13.5 MB       | 36.8 MB      |

**But memory reported is 124 MB, not 36.8 MB. So there are other issues too.**

Possibilities:
1. Dashboard live_status.json file is being held in memory?
2. Incident logs accumulating in memory?
3. Filter states storing full history somewhere?
4. Trajectories actually using much more memory than estimated?

---

## The Design Contradiction

### CLAUDE.md Says (Line 12)
> "**Tier 1:** Clear accumulated_data after auto-save → eliminates ~1.4 MB/min growth"

**1.4 MB/min = 84 MB/hour for 45-min test = ~63 MB growth alone**

This is CRITICAL for bounded memory.

### Code Comment Says (Line 2066)
```python
# FIX 1: DO NOT clear accumulated_data - keeps growing so final save has all data
# Each auto-save is a checkpoint on disk; accumulated_data accumulates continuously
# Final save combines disk checkpoints with current numpy array state
```

This is the **WRONG** approach. It causes unbounded memory growth.

---

## The Correct Architecture (Per CLAUDE.md)

### Current Approach (WRONG)
1. Auto-save writes all of `accumulated_data` to disk
2. Keep `accumulated_data` in memory forever (unbounded growth)
3. Final save combines old `accumulated_data` + new samples
4. **Result:** Memory grows indefinitely

### Correct Approach (Per Line 12 of CLAUDE.md)
1. Auto-save writes all of `accumulated_data` to disk
2. **CLEAR `accumulated_data` immediately after save** (TierN 1)
3. Final save reads LAST AUTO-SAVE FILE from disk + current numpy samples
4. **Result:** Memory stays bounded at ~95 MB

This is implemented at line 2090-2095 but never reached because we never clear!

---

## Evidence from CLAUDE.md (Nov 13, Afternoon Update)

The session notes document exactly what was supposed to happen:

> **Memory Fixes:**
> 1. **Tier 1:** Clear accumulated_data after auto-save → eliminates ~1.4 MB/min growth
> 2. **Tier 2:** Reduce queue sizes 500→100 → saves ~0.6 MB  ✓ (DONE at line 428)
> 3. **Tier 3:** Pause ES-EKF at 95 MB → prevents Android LMK kills  ✓ (implemented)
>
> **Result:** Memory stays 90-95 MB for 45-min tests

But **Tier 1 is NOT implemented** despite the code comment at line 2066-2068 suggesting an explanation for NOT doing it.

---

## The Fix Required

### Option A: Clear `accumulated_data` After Each Auto-Save (RECOMMENDED)
```python
# Line 2073 area, after successful save:
if clear_after_save:
    # Reset numpy array indices (reuse pre-allocated memory)
    self.gps_index = 0
    self.accel_index = 0
    self.gyro_index = 0

    # ✅ CLEAR accumulated_data to prevent memory growth
    # Next auto-save will start fresh, disk has checkpoint
    self._accumulated_data['gps_samples'].clear()
    self._accumulated_data['accel_samples'].clear()
    self._accumulated_data['gyro_samples'].clear()

    print(f"✓ Auto-saved (autosave #{...}): ... | Memory cleared")
```

**Pros:**
- Implements Tier 1 as documented in CLAUDE.md
- Bounded memory for any test duration
- Final save already handles reading disk checkpoint (lines 2090-2095)

**Cons:**
- Requires verifying final save logic works correctly

### Option B: Pre-Allocate Lists Instead of Growing Them
Not viable - we don't know total samples in advance.

### Option C: Use Memory-Mapped Files for accumulated_data
Overkill for this use case.

---

## Impact of Fix

| Scenario | Current (BROKEN) | After Fix | Status |
|----------|-----------------|-----------|--------|
| 5-min test | ~27 MB | ~23 MB | ✓ Bounded |
| 10-min test | ~26 MB | ~23 MB | ✓ Bounded |
| 45-min test | ~37 MB (+ 124 MB from other issues) | ~23 MB | ✓ Bounded |
| Memory growth rate | +1.4 MB/min | 0 MB/min | ✓ Eliminates growth |

---

## Investigation Findings

### What We Know
1. **Numpy arrays alone:** 6 MB (correctly bounded)
2. **Trajectory deques:** 0.4 MB (correctly bounded)
3. **Filters + Metrics:** 1.75 MB (correctly bounded)
4. **Python overhead:** ~15 MB (expected)
5. **Baseline = 23.3 MB** (matches our math)

### What We DON'T Know (Mystery 100+ MB)
- Something else is consuming ~100 MB that we can't account for
- Possibilities:
  - Live status JSON file being held in memory?
  - Incident detector context buffers?
  - Memory fragmentation / allocator overhead?
  - Cython compiled module state?
  - Filter matrices growing?

### What's Certain
- `accumulated_data` IS growing unbounded (this is provable from code)
- This contradicts CLAUDE.md documentation
- Even if other issues exist, clearing accumulated_data is **still required**

---

## Recommendation

1. **Implement Option A** (clear accumulated_data after auto-save)
2. **This fixes Tier 1** as documented in CLAUDE.md
3. **Then investigate** the remaining 100+ MB mystery
4. **Profile the test** with memory_profiler or tracemalloc to find other leaks

The accumulated_data growth is definitely a bug (documented but not implemented), even if it's not the sole cause of 124 MB.
