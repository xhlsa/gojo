# Numba JIT Optimization Plan for Gojo Motion Trackers

## Executive Summary
Strategy: Identify CPU-intensive hot paths and apply Numba `@jit` decorators for 10-30x speedups on critical calculations. Zero-risk approach: if Numba doesn't work on a function, just remove the decorator.

**Expected Results:**
- 10-50% reduction in overall CPU usage
- Maintains Python structure (no refactoring needed)
- Battery life improvement (lower CPU = lower drain)
- No installation complexity (pip install numba)

---

## Phase 1: Profiling & Bottleneck Identification

### Analysis Method
Run each tracker with built-in profiling:
```bash
# Profile motion_tracker_v2
python -m cProfile -s cumulative motion_tracker_v2/motion_tracker_v2.py --test 2>&1 | head -50

# Profile kalman tracker
python -m cProfile -s cumulative motion_tracker_kalman/motion_tracker_kalman.py --test 2>&1 | head -50
```

### Identified Bottlenecks (without profiling)

Based on code analysis, here are the compute-intensive functions:

#### **TIER 1: Highest ROI (Pure Math, Called Frequently)**

**1. AccelerationCalculator.calculate_motion_magnitude()**
- **File:** `motion_tracker_v2/accel_calculator.py:41-70`
- **Frequency:** ~50 Hz (every accel sample)
- **Operations:** sqrt() × 2, bias subtraction × 3
- **Expected speedup:** 10-15x
- **Why:** Pure math with no I/O or complex objects
- **Cost (no optimization):** 50 * 0.1ms = 5ms/sec CPU

```python
# Current (slow)
def calculate_motion_magnitude(self, accel_data):
    x = accel_data.get('x', 0) - self.bias_x
    y = accel_data.get('y', 0) - self.bias_y
    z = accel_data.get('z', 0) - self.bias_z
    total_magnitude = math.sqrt(x**2 + y**2 + z**2)
    motion_magnitude = total_magnitude - self.gravity_magnitude
    return max(0, motion_magnitude)

# Proposed (with Numba JIT)
@jit(nopython=True)
def _calculate_magnitude_jit(x, y, z, bias_x, bias_y, bias_z, gravity_mag):
    """JIT-compiled inner loop"""
    cx = x - bias_x
    cy = y - bias_y
    cz = z - bias_z
    total = math.sqrt(cx*cx + cy*cy + cz*cz)
    motion = total - gravity_mag
    return max(0.0, motion)

def calculate_motion_magnitude(self, accel_data):
    x = accel_data.get('x', 0)
    y = accel_data.get('y', 0)
    z = accel_data.get('z', 0)
    return self._calculate_magnitude_jit(x, y, z,
                                         self.bias_x, self.bias_y, self.bias_z,
                                         self.gravity_magnitude)
```

**2. SensorFusion.haversine_distance()**
- **File:** `motion_tracker_v2/motion_tracker_v2.py:81-94` or `motion_tracker_kalman/motion_tracker_kalman.py:284-297`
- **Frequency:** ~1 Hz (GPS updates, less frequent)
- **Operations:** radians() × 4, sin() × 2, cos() × 2, atan2() × 1, sqrt() × 1
- **Expected speedup:** 5-10x
- **Why:** Pure trigonometry with no I/O
- **Cost (no optimization):** 1 * 0.5ms = 0.5ms/sec CPU (minor but easy win)

```python
# Current
def haversine_distance(self, lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi/2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

# Proposed with JIT
@jit(nopython=True)
def _haversine_jit(lat1, lon1, lat2, lon2):
    """JIT-compiled haversine"""
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi/2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c
```

**3. _read_stream() JSON Parsing Loop**
- **File:** `motion_tracker_v2/motion_tracker_v2.py:322-368` or Kalman equivalent
- **Frequency:** ~50 Hz (every sensor line)
- **Operations:** brace counting loop, string concatenation
- **Expected speedup:** 5-8x
- **Why:** Tight loop with character-by-character operations
- **Cost (no optimization):** 50 * 0.2ms = 10ms/sec CPU

```python
# Current (Python loop)
json_buffer = ""
brace_count = 0
for line in self.process.stdout:
    if self.stop_event.is_set():
        break
    json_buffer += line
    brace_count += line.count('{') - line.count('}')
    if brace_count == 0 and json_buffer.strip():
        # parse JSON...

# Proposed with separate JIT function
@jit(nopython=True)
def _count_braces_jit(line_str):
    """Count net braces in a line"""
    count = 0
    for char in line_str:
        if char == '{':
            count += 1
        elif char == '}':
            count -= 1
    return count

# Use in main loop
for line in self.process.stdout:
    brace_count += _count_braces_jit(line)
    # ... rest of logic in Python
```

---

#### **TIER 2: Medium ROI (Math + State, Called Frequently)**

**4. SensorFusion.update_accelerometer()**
- **File:** `motion_tracker_v2/motion_tracker_v2.py:152-186`
- **Frequency:** ~50 Hz
- **Operations:** arithmetic, comparisons, state updates
- **Challenge:** Mixes math with state modifications (harder to JIT)
- **Expected speedup:** 3-5x (if we extract math to separate function)
- **Approach:** Extract numerical integration to separate JIT function

```python
# Current
def update_accelerometer(self, accel_magnitude):
    with self.lock:
        current_time = time.time()
        # ... 34 lines mixing state + math ...
        if abs(accel_magnitude) < self.stationary_threshold:
            accel_magnitude = 0
        self.accel_velocity += accel_magnitude * dt
        self.accel_velocity = max(0, self.accel_velocity)
        self.distance += self.accel_velocity * dt

# Proposed: Extract core math
@jit(nopython=True)
def _integrate_accel_jit(accel_mag, accel_velocity, distance, dt, threshold):
    """Pure math integration - no state"""
    if abs(accel_mag) < threshold:
        accel_mag = 0.0
    new_velocity = accel_velocity + accel_mag * dt
    new_velocity = max(0.0, new_velocity)
    new_distance = distance + new_velocity * dt
    return new_velocity, new_distance

def update_accelerometer(self, accel_magnitude):
    with self.lock:
        dt = current_time - self.last_accel_time
        # Call JIT function for math
        self.accel_velocity, self.distance = self._integrate_accel_jit(
            accel_magnitude, self.accel_velocity, self.distance,
            dt, self.stationary_threshold
        )
```

**5. KalmanSensorFusion._estimate_2d_accel()**
- **File:** `motion_tracker_kalman/motion_tracker_kalman.py:388-425`
- **Frequency:** ~50 Hz (every accel update)
- **Operations:** 2D vector math, magnitude, division
- **Expected speedup:** 5-8x
- **Approach:** Extract to JIT-compiled function

```python
# Current lines 413-425
vel_x = self.kf.x[1, 0]
vel_y = self.kf.x[4, 0]
vel_mag = math.sqrt(vel_x**2 + vel_y**2)

if vel_mag > 0.1:
    accel_x = accel_magnitude * (vel_x / vel_mag)
    accel_y = accel_magnitude * (vel_y / vel_mag)
else:
    accel_x = accel_magnitude * math.sqrt(0.5)
    accel_y = accel_magnitude * math.sqrt(0.5)

# Proposed JIT version
@jit(nopython=True)
def _estimate_2d_accel_jit(accel_mag, vel_x, vel_y):
    """Estimate 2D acceleration from magnitude and velocity direction"""
    vel_mag_sq = vel_x*vel_x + vel_y*vel_y
    if vel_mag_sq > 0.01:  # vel_mag > 0.1 (squared to avoid sqrt)
        vel_mag = math.sqrt(vel_mag_sq)
        accel_x = accel_mag * (vel_x / vel_mag)
        accel_y = accel_mag * (vel_y / vel_mag)
    else:
        sqrt_half = 0.7071067811865476  # sqrt(0.5), precomputed
        accel_x = accel_mag * sqrt_half
        accel_y = accel_mag * sqrt_half
    return accel_x, accel_y
```

---

#### **TIER 3: Lower ROI (Already Optimized or Less Frequent)**

**6. Kalman Filter predict/update**
- **File:** `motion_tracker_kalman/motion_tracker_kalman.py:358-371`
- **Frequency:** ~50 Hz
- **Challenge:** Already using NumPy (matrix operations), Numba won't help much
- **Status:** Skip for now (numpy is already compiled)
- **Potential:** Only 10-20% speedup, not worth the complexity

---

## Phase 2: Implementation Steps (Sequential)

### Step 1: Setup
```bash
pip install numba
```

Create `motion_tracker_v2/accel_jit.py`:
```python
from numba import jit
import math

# All JIT-compiled functions in one module
# Makes it easy to find and test them

@jit(nopython=True)
def calculate_magnitude_jit(x, y, z, bias_x, bias_y, bias_z, gravity_mag):
    cx = x - bias_x
    cy = y - bias_y
    cz = z - bias_z
    total = math.sqrt(cx*cx + cy*cy + cz*cz)
    motion = total - gravity_mag
    return max(0.0, motion)

@jit(nopython=True)
def haversine_jit(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi/2)**2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

@jit(nopython=True)
def count_braces_jit(line_str):
    count = 0
    for char in line_str:
        if char == '{':
            count += 1
        elif char == '}':
            count -= 1
    return count

@jit(nopython=True)
def integrate_accel_jit(accel_mag, accel_velocity, distance, dt, threshold):
    if abs(accel_mag) < threshold:
        accel_mag = 0.0
    new_velocity = accel_velocity + accel_mag * dt
    new_velocity = max(0.0, new_velocity)
    new_distance = distance + new_velocity * dt
    return new_velocity, new_distance

@jit(nopython=True)
def estimate_2d_accel_jit(accel_mag, vel_x, vel_y):
    vel_mag_sq = vel_x*vel_x + vel_y*vel_y
    if vel_mag_sq > 0.01:
        vel_mag = math.sqrt(vel_mag_sq)
        accel_x = accel_mag * (vel_x / vel_mag)
        accel_y = accel_mag * (vel_y / vel_mag)
    else:
        sqrt_half = 0.7071067811865476
        accel_x = accel_mag * sqrt_half
        accel_y = accel_mag * sqrt_half
    return accel_x, accel_y
```

### Step 2: Test Priority (Highest ROI First)

**Test 2a: AccelerationCalculator Speedup**
```bash
# Benchmark calculation_motion_magnitude before/after
python3 << 'EOF'
import time
from motion_tracker_v2.accel_calculator import AccelerationCalculator

calc = AccelerationCalculator(
    gravity_magnitude=9.81,
    bias_x=0.1, bias_y=0.05, bias_z=0.0,
    method='magnitude'
)

# Test data (1000 samples)
test_data = [
    {'x': 10.0 + i*0.01, 'y': 9.8, 'z': 0.1}
    for i in range(1000)
]

# Benchmark
start = time.time()
for _ in range(100):
    for data in test_data:
        calc.calculate_motion_magnitude(data)
elapsed = time.time() - start

print(f"1M calculations: {elapsed:.3f}s")
print(f"Rate: {1e6/elapsed:.0f} calc/s")
EOF
```

**Test 2b: Haversine Speedup**
```bash
# Benchmark haversine distance
python3 << 'EOF'
import time
from motion_tracker_v2.motion_tracker_v2 import SensorFusion

fusion = SensorFusion()

# Test 1000 samples
start = time.time()
for _ in range(10000):
    fusion.haversine_distance(37.7749, -122.4194, 37.7750, -122.4193)
elapsed = time.time() - start

print(f"10k haversine calls: {elapsed:.3f}s")
print(f"Rate: {10000/elapsed:.0f} calls/s")
EOF
```

### Step 3: Production Deployment

After confirming speedups:

1. **Add to motion_tracker_v2.py:**
   ```python
   try:
       from accel_jit import (
           calculate_magnitude_jit, haversine_jit,
           count_braces_jit, integrate_accel_jit
       )
       HAS_NUMBA = True
   except ImportError:
       HAS_NUMBA = False
       print("⚠ Numba not available, using pure Python")
   ```

2. **Update AccelerationCalculator:**
   ```python
   def calculate_motion_magnitude(self, accel_data):
       if HAS_NUMBA:
           x = accel_data.get('x', 0)
           y = accel_data.get('y', 0)
           z = accel_data.get('z', 0)
           return calculate_magnitude_jit(x, y, z,
                                         self.bias_x, self.bias_y, self.bias_z,
                                         self.gravity_magnitude)
       else:
           # Fallback to pure Python
           x = accel_data.get('x', 0) - self.bias_x
           # ... original code ...
   ```

3. **Update SensorFusion.haversine_distance():**
   ```python
   def haversine_distance(self, lat1, lon1, lat2, lon2):
       if HAS_NUMBA:
           return haversine_jit(lat1, lon1, lat2, lon2)
       else:
           # Original implementation
   ```

4. **Update _read_stream() brace counting:**
   ```python
   for line in self.process.stdout:
       if HAS_NUMBA:
           brace_count += count_braces_jit(line)
       else:
           brace_count += line.count('{') - line.count('}')
   ```

---

## Phase 3: Expected Results & Metrics

### Before Optimization (Estimated)

| Operation | Frequency | Time per call | Total per sec |
|-----------|-----------|---------------|---------------|
| calculate_motion_magnitude | 50 Hz | 0.2 ms | 10 ms |
| haversine_distance | 1 Hz | 0.5 ms | 0.5 ms |
| brace_counting | 50 Hz | 0.2 ms | 10 ms |
| integrate_accel | 50 Hz | 0.1 ms | 5 ms |
| **TOTAL** | | | **25.5 ms/sec** |

### After Optimization (Estimated)

| Operation | Speedup | New Total |
|-----------|---------|-----------|
| calculate_motion_magnitude | 12x | 0.8 ms |
| haversine_distance | 8x | 0.06 ms |
| brace_counting | 6x | 1.6 ms |
| integrate_accel | 4x | 1.2 ms |
| **TOTAL REDUCTION** | **~8x** | **3.6 ms/sec** |

**Result:** 25.5 → 3.6 ms/sec CPU = **86% reduction in these hot paths**

### Estimated Battery Impact
- CPU usage: 10% → 2% (during active tracking)
- Battery life on 5-hour drive: +20-30 minutes

---

## Risk Assessment

### Numba Limitations (and Workarounds)

| Risk | Mitigation |
|------|-----------|
| Numba doesn't support Python objects | Use only scalars/arrays in JIT functions |
| First call has ~1 second compilation overhead | Compile on startup, not during tracking |
| String operations may not work | Extract to Python wrapper |
| Threading/locks not JIT-compatible | Keep state updates in Python |

### Fallback Strategy
```python
if HAS_NUMBA:
    result = fast_jit_version()
else:
    result = slow_python_version()  # Same logic, no JIT
```

---

## Testing Plan

### Unit Tests
```python
# Test that JIT output matches Python output
def test_magnitude_equivalence():
    accel_data = {'x': 10.0, 'y': 9.8, 'z': 0.1}

    # Python version
    python_result = calc.calculate_motion_magnitude(accel_data)

    # JIT version
    jit_result = calculate_magnitude_jit(10.0, 9.8, 0.1, 0.1, 0.05, 0.0, 9.81)

    assert abs(python_result - jit_result) < 1e-6
```

### Integration Tests
```bash
# Run motion tracker --test with Numba enabled
# Verify output files match (same motion data)
# Check CPU usage is lower

python motion_tracker_v2/motion_tracker_v2.py --test
# Should see: "✓ Numba JIT active (5 functions compiled)"
```

### Performance Benchmarks
```bash
# Run profiler before/after
python -m cProfile -s cumulative motion_tracker_v2/motion_tracker_v2.py --test
```

---

## Implementation Timeline

| Phase | Effort | Outcome |
|-------|--------|---------|
| **Phase 1** | 30 min | Profiling identifies actual bottlenecks |
| **Phase 2a** | 1 hour | Create accel_jit.py with all JIT functions |
| **Phase 2b** | 1 hour | Add conditional imports to trackers |
| **Phase 2c** | 1 hour | Update all hot paths to use JIT |
| **Phase 3** | 30 min | Benchmark & verify speedups |
| **Total** | ~4 hours | Full optimization + testing |

---

## Future Optimizations (If Needed)

1. **Cython recompile** - If Numba not sufficient, recompile accel_processor.pyx with more aggressive optimizations
2. **NumPy vectorization** - If we add batch processing (multiple sensors)
3. **Rust FFI** - If we need 50%+ more speedup (rewrite hottest functions in Rust)
4. **PyPy** - Drop-in Python interpreter replacement, 3-7x faster for numeric code

---

## Estimated Impact by Use Case

### 5-hour highway drive
- Current CPU: ~12% (50 Hz accel + 1 Hz GPS)
- With Numba: ~2%
- Battery saved: ~20-30 minutes

### 30-minute walk/jog
- Current CPU: ~8% (same rates, less GPS accuracy)
- With Numba: ~1.5%
- Battery saved: ~3-5 minutes

### Real-time web dashboard (if added)
- Numba helps even more (reduces Python GIL contention)
- Could enable 100 Hz accel sampling without CPU spike

