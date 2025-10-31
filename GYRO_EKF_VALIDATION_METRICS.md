# 13D Gyro-EKF Validation Metrics Framework

**Purpose:** Verify the 13D bias-aware Extended Kalman Filter is correctly calculating orientation, learning gyro bias, reducing error, and improving incident detection accuracy.

**State Vector:** [x, y, vx, vy, ax, ay, q0, q1, q2, q3, bx, by, bz]
- Position (x, y): meters
- Velocity (vx, vy): m/s
- Acceleration (ax, ay): m/s²
- Quaternion (q0, q1, q2, q3): orientation (unit norm)
- Gyro bias (bx, by, bz): rad/s drift correction

---

## 1. BIAS CONVERGENCE METRICS

**Goal:** Prove the filter is learning gyro bias correctly (not stuck at zero, converging to stable values).

### 1.1 Bias Magnitude Tracking

**Definition:** Measure gyro bias vector magnitude over time: `|bias| = sqrt(bx² + by² + bz²)`

**How to Calculate:**
```python
bias = state[10:13]  # [bx, by, bz]
bias_magnitude = np.linalg.norm(bias)
```

**Expected Range:**
- **Initial (t=0):** ~0.000 rad/s (starts at zero)
- **Converged (t>30s):** 0.001 - 0.05 rad/s (typical smartphone gyro drift)
- **Stable (t>2min):** ±10% variation from converged value

**Warning Thresholds:**
- Stuck at zero (bias < 0.0001 rad/s after 60s) → Bias not learning
- Too high (bias > 0.1 rad/s) → Sensor issue or model problem
- Oscillating (std dev > 50% of mean after 2min) → Non-convergent

**How to Collect:**
```python
# In gyro update loop
bias_history.append({
    'timestamp': time.time() - start_time,
    'bx': state[10],
    'by': state[11],
    'bz': state[12],
    'magnitude': np.linalg.norm(state[10:13])
})
```

**Validation Test:**
1. Place phone flat on table (stationary)
2. Record gyro bias for 60 seconds
3. Plot bias magnitude vs time
4. Check: bias increases from 0 and plateaus within 30-60s

**Success Criteria:**
- Bias magnitude > 0.001 rad/s after 30s (not stuck at zero)
- Bias magnitude stable (std dev < 0.005 rad/s) after 60s
- Bias change rate < 0.0001 rad/s per second after convergence

---

### 1.2 Bias Convergence Rate

**Definition:** How quickly bias estimates stabilize (time to reach 90% of final value).

**How to Calculate:**
```python
def compute_convergence_time(bias_history):
    """Find time when bias reaches 90% of final stable value"""
    final_bias = np.mean([h['magnitude'] for h in bias_history[-20:]])  # Last 20 samples
    target = 0.9 * final_bias

    for i, sample in enumerate(bias_history):
        if sample['magnitude'] >= target:
            return sample['timestamp']
    return None
```

**Expected Range:**
- **Fast convergence:** <10 seconds (ideal)
- **Normal convergence:** 10-30 seconds (acceptable)
- **Slow convergence:** 30-60 seconds (tuning needed)

**Warning Thresholds:**
- Never converges (>120s) → Model broken
- Too fast (<5s) → May be noise, not true bias

**Success Criteria:**
- Bias reaches 90% of final value within 30 seconds
- Final value remains stable (±10%) for remaining test duration

---

### 1.3 Per-Axis Bias Stability

**Definition:** Track individual bias components (bx, by, bz) to detect axis-specific issues.

**How to Calculate:**
```python
# After convergence (t > 60s)
bx_samples = [h['bx'] for h in bias_history if h['timestamp'] > 60]
by_samples = [h['by'] for h in bias_history if h['timestamp'] > 60]
bz_samples = [h['bz'] for h in bias_history if h['timestamp'] > 60]

bx_std = np.std(bx_samples)
by_std = np.std(by_samples)
bz_std = np.std(bz_samples)
```

**Expected Range:**
- **Per-axis std dev:** <0.005 rad/s after convergence
- **Typical magnitudes:** 0.001 - 0.02 rad/s per axis

**Warning Thresholds:**
- One axis stuck at zero while others have bias → Hardware issue
- One axis std dev >> others → Noisy sensor on that axis

**Success Criteria:**
- All three axes show non-zero bias (>0.0005 rad/s)
- All three axes have similar stability (std dev within 2x of each other)

---

### 1.4 Bias-Corrected Gyro Residual (Stationary)

**Definition:** Measure gyro noise after subtracting learned bias when phone is stationary.

**How to Calculate:**
```python
# When stationary
residual_x = gyro_x - bias_x
residual_y = gyro_y - bias_y
residual_z = gyro_z - bias_z
residual_magnitude = np.sqrt(residual_x**2 + residual_y**2 + residual_z**2)

# Collect 100+ samples while stationary
residual_std = np.std(residual_samples)
```

**Expected Range:**
- **Stationary residual std dev:** <0.01 rad/s (~0.57°/s)
- **Typical mean:** ~0.0 rad/s (bias-corrected)

**Warning Thresholds:**
- Residual std dev > 0.02 rad/s → Bias not fully learned
- Residual mean >> 0 → Bias estimate wrong

**Success Criteria:**
- After 60s stationary: residual std dev < 0.01 rad/s
- Residual mean within ±0.005 rad/s of zero

---

## 2. QUATERNION HEALTH METRICS

**Goal:** Verify quaternion remains mathematically valid (unit norm, stable kinematics).

### 2.1 Quaternion Normalization Check

**Definition:** Quaternion must satisfy: `q0² + q1² + q2² + q3² = 1`

**How to Calculate:**
```python
q = state[6:10]  # [q0, q1, q2, q3]
q_norm = np.linalg.norm(q)
q_norm_error = abs(q_norm - 1.0)
```

**Expected Range:**
- **Ideal norm:** 1.0 exactly
- **Acceptable tolerance:** 1.0 ± 0.001 (0.1% error)
- **Warning threshold:** 1.0 ± 0.01 (1% error)

**Warning Thresholds:**
- Norm error > 0.01 → Numerical instability
- Norm error > 0.1 → Quaternion diverged (CRITICAL)

**How to Collect:**
```python
quaternion_health.append({
    'timestamp': time.time() - start_time,
    'q0': state[6],
    'q1': state[7],
    'q2': state[8],
    'q3': state[9],
    'norm': q_norm,
    'norm_error': q_norm_error
})
```

**Validation Test:**
1. Run filter for 10 minutes
2. Plot quaternion norm vs time
3. Check: norm stays within 1.0 ± 0.001

**Success Criteria:**
- Quaternion norm error < 0.001 throughout entire test
- No sudden jumps in norm (change < 0.01 per update)

---

### 2.2 Quaternion Rate of Change

**Definition:** Measure how fast quaternion is changing (should match gyro magnitude).

**How to Calculate:**
```python
# Between consecutive updates
q_prev = quaternion_history[-2]['q']
q_curr = quaternion_history[-1]['q']
dt = quaternion_history[-1]['timestamp'] - quaternion_history[-2]['timestamp']

# Quaternion difference magnitude
dq = q_curr - q_prev
dq_magnitude = np.linalg.norm(dq)
dq_rate = dq_magnitude / dt  # Quaternion change per second
```

**Expected Range:**
- **Stationary:** dq_rate < 0.01 per second
- **Rotating (1 rad/s):** dq_rate ~ 0.5 per second (approximate, non-linear)
- **Fast rotation (5 rad/s):** dq_rate ~ 2.5 per second

**Warning Thresholds:**
- dq_rate > 10 per second → Numerical explosion
- dq_rate = 0 for >1 second when gyro active → Quaternion frozen

**Success Criteria:**
- dq_rate correlates with gyro magnitude (higher gyro → higher dq_rate)
- dq_rate bounded (<5 per second for normal vehicle motion)

---

### 2.3 Quaternion to Euler Angle Consistency

**Definition:** Convert quaternion to Euler angles (roll, pitch, yaw) and verify they're physically plausible.

**How to Calculate:**
```python
def quaternion_to_euler(q0, q1, q2, q3):
    """
    Convert quaternion to Euler angles (roll, pitch, yaw) in radians.

    Returns:
        roll: rotation about X-axis (rad)
        pitch: rotation about Y-axis (rad)
        yaw: rotation about Z-axis (rad)
    """
    # Roll (X-axis rotation)
    sinr_cosp = 2 * (q0*q1 + q2*q3)
    cosr_cosp = 1 - 2 * (q1**2 + q2**2)
    roll = np.arctan2(sinr_cosp, cosr_cosp)

    # Pitch (Y-axis rotation)
    sinp = 2 * (q0*q2 - q3*q1)
    if abs(sinp) >= 1:
        pitch = np.copysign(np.pi/2, sinp)  # Use 90° if out of range
    else:
        pitch = np.arcsin(sinp)

    # Yaw (Z-axis rotation)
    siny_cosp = 2 * (q0*q3 + q1*q2)
    cosy_cosp = 1 - 2 * (q2**2 + q3**2)
    yaw = np.arctan2(siny_cosp, cosy_cosp)

    return roll, pitch, yaw
```

**Expected Range:**
- **Vehicle motion:**
  - Roll: -30° to +30° (-0.52 to +0.52 rad) [normal driving]
  - Pitch: -30° to +30° [braking/acceleration]
  - Yaw: -180° to +180° [full rotation range]

**Warning Thresholds:**
- Roll > 60° (1.05 rad) → Vehicle tilting (rollover risk)
- Pitch > 45° (0.79 rad) → Steep hill or crash
- Any angle = NaN or Inf → Quaternion corrupted

**Success Criteria:**
- Euler angles stay within vehicle motion ranges
- Angles change smoothly (no jumps >30° per second)

---

## 3. ROTATION TRACKING METRICS

**Goal:** Verify the filter can accurately detect and measure rotations.

### 3.1 Static Rotation Tests

**Definition:** Manually rotate phone and measure detected angle change.

**Validation Tests:**

**Test 3.1a: 90° Yaw Rotation (Right Turn)**
1. Place phone flat on table, aligned with reference
2. Record initial quaternion
3. Rotate phone 90° clockwise (yaw)
4. Record final quaternion
5. Convert to Euler angles and measure yaw change

**Expected:** Yaw change = 90° ± 5° (1.57 ± 0.09 rad)

**Test 3.1b: 45° Pitch Rotation (Tilt Forward)**
1. Phone upright (vertical)
2. Record initial quaternion
3. Tilt phone forward 45°
4. Measure pitch change

**Expected:** Pitch change = 45° ± 5° (0.79 ± 0.09 rad)

**Test 3.1c: 90° Roll Rotation (Phone on Side)**
1. Phone flat
2. Roll phone 90° (landscape orientation)
3. Measure roll change

**Expected:** Roll change = 90° ± 5° (1.57 ± 0.09 rad)

**How to Calculate:**
```python
# Before rotation
euler_before = quaternion_to_euler(*state_before[6:10])

# After rotation (allow 3 seconds to settle)
time.sleep(3)
euler_after = quaternion_to_euler(*state_after[6:10])

# Angle changes
roll_change = euler_after[0] - euler_before[0]
pitch_change = euler_after[1] - euler_before[1]
yaw_change = euler_after[2] - euler_before[2]

# Handle wrap-around for yaw (±180°)
if yaw_change > np.pi:
    yaw_change -= 2*np.pi
elif yaw_change < -np.pi:
    yaw_change += 2*np.pi
```

**Success Criteria:**
- Detected angle within ±5° of true rotation
- Angle settles within 3 seconds
- No overshoot (angle doesn't exceed rotation then settle back)

---

### 3.2 Rotation Velocity Correlation

**Definition:** Verify gyro magnitude matches quaternion rate of change.

**How to Calculate:**
```python
# Theoretical: quaternion rate should relate to angular velocity
# |dq/dt| ≈ 0.5 * |ω| * |q| (where |q| = 1)
gyro_magnitude = np.sqrt(gyro_x**2 + gyro_y**2 + gyro_z**2)

# Quaternion change rate (from previous metric)
expected_dq_rate = 0.5 * gyro_magnitude  # Approximate

correlation = np.corrcoef(gyro_magnitude_history, dq_rate_history)[0, 1]
```

**Expected Range:**
- **Correlation coefficient:** >0.7 (strong positive correlation)
- **Scaling factor:** dq_rate ≈ 0.5 * gyro_magnitude

**Success Criteria:**
- Correlation > 0.7 between gyro magnitude and quaternion change rate
- When gyro = 0 (stationary), dq_rate ≈ 0

---

### 3.3 GPS Heading vs EKF Heading (Yaw Comparison)

**Definition:** Compare GPS-reported bearing with EKF quaternion-derived yaw angle.

**How to Calculate:**
```python
# GPS heading (from termux-location 'bearing' field)
gps_heading = gps_data['bearing']  # degrees, 0 = North

# EKF heading (from quaternion yaw angle)
roll, pitch, yaw = quaternion_to_euler(*state[6:10])
ekf_heading = np.degrees(yaw)  # Convert rad to degrees

# Adjust to 0-360 range
if ekf_heading < 0:
    ekf_heading += 360

# Heading error
heading_error = abs(gps_heading - ekf_heading)
if heading_error > 180:
    heading_error = 360 - heading_error  # Wrap-around
```

**Expected Range:**
- **Heading error:** <15° during straight driving
- **Convergence time:** <30 seconds after starting motion

**Warning Thresholds:**
- Heading error > 30° after 1 minute → EKF yaw diverging
- Heading error increasing over time → Bias not learned correctly

**Success Criteria:**
- Heading error < 15° after convergence (60s of motion)
- Heading error decreases over time (EKF learns correct orientation)

---

## 4. GYRO NOISE & BIAS METRICS

**Goal:** Quantify gyro signal quality and bias learning performance.

### 4.1 Stationary Gyro Statistics

**Definition:** Characterize gyro noise when phone is completely still.

**How to Collect:**
1. Place phone flat on table (stationary)
2. Collect 200+ gyro samples (10 seconds at 20 Hz)
3. Calculate statistics

**How to Calculate:**
```python
# Raw gyro (before bias correction)
gyro_x_samples = [s['gyro_x'] for s in stationary_samples]
gyro_y_samples = [s['gyro_y'] for s in stationary_samples]
gyro_z_samples = [s['gyro_z'] for s in stationary_samples]

raw_std_x = np.std(gyro_x_samples)
raw_std_y = np.std(gyro_y_samples)
raw_std_z = np.std(gyro_z_samples)
raw_mean_magnitude = np.mean([np.sqrt(x**2 + y**2 + z**2) for x,y,z in zip(gyro_x_samples, gyro_y_samples, gyro_z_samples)])

# After bias correction
residuals = [(x - bias_x, y - bias_y, z - bias_z) for x, y, z in zip(gyro_x_samples, gyro_y_samples, gyro_z_samples)]
residual_std = np.std([np.sqrt(rx**2 + ry**2 + rz**2) for rx, ry, rz in residuals])
```

**Expected Range:**
- **Raw std dev (per axis):** 0.005 - 0.02 rad/s
- **Raw mean magnitude:** 0.01 - 0.05 rad/s (includes bias)
- **After bias correction std dev:** <0.01 rad/s

**Success Criteria:**
- Bias-corrected residual std dev < raw std dev (bias removal reduces noise)
- Bias-corrected mean magnitude near zero (<0.005 rad/s)

---

### 4.2 Gyro Bias Drift Rate

**Definition:** How fast gyro bias changes over time (should be very slow).

**How to Calculate:**
```python
# After convergence (t > 60s), measure bias change rate
bias_samples = [h['magnitude'] for h in bias_history if h['timestamp'] > 60]
timestamps = [h['timestamp'] for h in bias_history if h['timestamp'] > 60]

# Linear regression: bias = drift_rate * time + offset
coefficients = np.polyfit(timestamps, bias_samples, deg=1)
drift_rate = coefficients[0]  # rad/s per second = rad/s²
```

**Expected Range:**
- **Drift rate:** <0.0001 rad/s per second (very slow)
- **Temperature-stable environment:** <0.00005 rad/s per second

**Warning Thresholds:**
- Drift rate > 0.001 rad/s per second → Sensor unstable or overheating
- Negative drift rate (decreasing bias) → Model problem

**Success Criteria:**
- Bias drift rate < 0.0001 rad/s per second
- Drift is monotonic (bias doesn't oscillate)

---

### 4.3 Innovation Magnitude (Gyro Measurement Residual)

**Definition:** Difference between measured gyro and predicted gyro (expected = bias).

**How to Calculate:**
```python
# In update_gyroscope method
z = np.array([gyro_x, gyro_y, gyro_z])  # Measurement
z_pred = state[10:13]  # Predicted measurement = bias
innovation = z - z_pred
innovation_magnitude = np.linalg.norm(innovation)
```

**Expected Range:**
- **Initial (bias not learned):** 0.01 - 0.1 rad/s (large)
- **After convergence:** <0.02 rad/s (small, mostly noise)

**Warning Thresholds:**
- Innovation > 0.1 rad/s after convergence → Model mismatch
- Innovation growing over time → Bias not tracking correctly

**Success Criteria:**
- Innovation magnitude decreases over first 60 seconds
- After convergence: innovation < 0.02 rad/s (mostly measurement noise)

---

## 5. FILTER ACCURACY COMPARISON METRICS

**Goal:** Prove EKF with gyro performs better than baseline filters.

### 5.1 Position Tracking Accuracy (vs GPS Ground Truth)

**Definition:** Compare EKF distance estimate to haversine GPS ground truth.

**How to Calculate:**
```python
# GPS ground truth (from test framework)
gps_distance = haversine_accumulate(gps_samples)

# EKF estimate
ekf_distance = state_distance  # From filter

# Complementary filter estimate
comp_distance = complementary_state_distance

# Errors
ekf_error_pct = abs(ekf_distance - gps_distance) / gps_distance * 100
comp_error_pct = abs(comp_distance - gps_distance) / gps_distance * 100

# Improvement
improvement = (comp_error_pct - ekf_error_pct) / comp_error_pct * 100
```

**Expected Range:**
- **EKF error:** <5% for drives >500m
- **Improvement over complementary:** >10%

**Success Criteria:**
- EKF error < complementary error (EKF more accurate)
- EKF error < 10% for drives >1km

---

### 5.2 Velocity Smoothness (Reduced Jitter)

**Definition:** EKF should produce smoother velocity estimates than complementary filter.

**How to Calculate:**
```python
# Standard deviation of velocity (lower = smoother)
ekf_velocities = [s['ekf_velocity'] for s in comparison_samples]
comp_velocities = [s['comp_velocity'] for s in comparison_samples]

ekf_velocity_std = np.std(ekf_velocities)
comp_velocity_std = np.std(comp_velocities)

smoothness_improvement = (comp_velocity_std - ekf_velocity_std) / comp_velocity_std * 100
```

**Expected Range:**
- **EKF velocity std dev:** <0.5 m/s
- **Smoothness improvement:** >20% vs complementary

**Success Criteria:**
- EKF velocity std dev < complementary std dev
- EKF velocity doesn't jump >1 m/s between consecutive GPS updates

---

### 5.3 Orientation Estimation Quality (Gyro Benefit)

**Definition:** With gyro enabled, EKF should track orientation better than accel-only.

**Comparison Tests:**

**Test 5.3a: EKF with Gyro vs EKF without Gyro**
Run two EKF filters in parallel:
- EKF_gyro: 13D state with gyroscope
- EKF_no_gyro: 6D state (GPS + accel only)

Compare:
1. Orientation estimate stability (gyro EKF should have valid quaternion)
2. Heading convergence (gyro EKF should match GPS bearing faster)
3. Rotation detection (gyro EKF should detect turns, no-gyro can't)

**Expected:**
- EKF with gyro provides valid orientation (quaternion norm = 1.0)
- EKF without gyro has no orientation (no quaternion states)
- Gyro EKF heading matches GPS bearing within 15°

**Success Criteria:**
- Gyro EKF provides valid quaternion (norm error < 0.001)
- Gyro EKF heading converges to GPS bearing within 30 seconds
- Gyro EKF detects turns (yaw changes correlate with vehicle turns)

---

### 5.4 Swerving Detection Confidence (Yaw Rate Measurement)

**Definition:** Can EKF detect swerving via yaw rate with >80% confidence?

**How to Calculate:**
```python
# Swerving threshold: >60°/sec yaw rate
# (from CLAUDE.md incident detection specs)
SWERVE_THRESHOLD = 60  # degrees/sec
SWERVE_THRESHOLD_RAD = np.radians(60)  # 1.047 rad/s

# From gyro measurement (bias-corrected)
yaw_rate = abs(gyro_z - bias_z)  # Z-axis is yaw

is_swerving = yaw_rate > SWERVE_THRESHOLD_RAD

# Confidence: inverse of measurement uncertainty
# Lower innovation → higher confidence
innovation_magnitude = compute_innovation_magnitude()
confidence = 1.0 / (1.0 + innovation_magnitude * 10)  # Scale to 0-1
```

**Expected Range:**
- **Normal driving yaw rate:** <0.5 rad/s (~30°/s)
- **Swerving yaw rate:** >1.0 rad/s (>60°/s)
- **Confidence during swerving:** >0.8 (80%)

**Validation Test:**
1. Drive straight: yaw rate should be near zero
2. Make sharp turn: yaw rate should exceed threshold
3. Check confidence > 0.8 during turn

**Success Criteria:**
- Swerving detected (yaw rate > threshold) during sharp turns
- Confidence > 0.8 when yaw rate > threshold
- No false positives during straight driving (<5% of stationary time)

---

## 6. INCIDENT DETECTION VALIDATION METRICS

**Goal:** Verify gyro improves real-world incident detection accuracy.

### 6.1 Hard Braking Pitch Angle Detection

**Definition:** Hard braking should cause nose-down pitch angle (negative pitch).

**How to Detect:**
```python
# Hard braking: deceleration >0.8g = 7.85 m/s²
accel_magnitude = compute_accel_magnitude()
is_hard_braking = accel_magnitude < -7.85  # Negative = braking

# Expected pitch angle change
roll, pitch, yaw = quaternion_to_euler(*state[6:10])
pitch_degrees = np.degrees(pitch)

# Hard braking → pitch should go negative (nose down)
expected_pitch_change = -5 to -20 degrees
```

**Expected Behavior:**
- **Before braking:** pitch ≈ 0° (level)
- **During hard braking:** pitch < -5° (nose down)
- **After braking:** pitch returns to ~0°

**Validation Test:**
1. Drive at steady speed
2. Record baseline pitch angle
3. Apply hard brakes (decel >0.8g)
4. Measure minimum pitch angle during braking
5. Check pitch change >5° downward

**Success Criteria:**
- Pitch angle decreases (goes negative) during hard braking
- Pitch change magnitude correlates with braking intensity
- Pitch returns to baseline after braking stops

---

### 6.2 Impact Direction Detection (Roll/Pitch Analysis)

**Definition:** Impacts should cause sudden roll/pitch changes indicating impact direction.

**How to Detect:**
```python
# Impact: sudden acceleration >1.5g = 14.7 m/s²
accel_magnitude = compute_accel_magnitude()
is_impact = abs(accel_magnitude) > 14.7

# Measure orientation at impact
roll, pitch, yaw = quaternion_to_euler(*state[6:10])

# Classify impact direction
if abs(roll) > abs(pitch):
    direction = "SIDE" + (" LEFT" if roll > 0 else " RIGHT")
elif pitch > 0:
    direction = "FRONT"
else:
    direction = "REAR"
```

**Expected Behavior:**
- **Front impact:** pitch > +10° (nose up)
- **Rear impact:** pitch < -10° (nose down)
- **Left side impact:** roll < -10° (tilt left)
- **Right side impact:** roll > +10° (tilt right)

**Validation Test:**
(Simulate with phone, not real vehicle impact!)
1. Drop phone on cushion (simulates impact)
2. Orient phone to simulate impact direction
3. Measure roll/pitch at moment of impact
4. Verify direction classification matches orientation

**Success Criteria:**
- Impact detected (accel > 1.5g)
- Direction classification matches phone orientation
- Confidence > 0.7 (based on quaternion stability)

---

### 6.3 Swerving Event Detection (Yaw Rate Threshold)

**Definition:** Detect swerving when yaw rate exceeds 60°/s.

**How to Detect:**
```python
# Swerving: yaw rate >60°/s = 1.047 rad/s
gyro_z_corrected = gyro_z - bias_z  # Bias-corrected yaw rate
is_swerving = abs(gyro_z_corrected) > 1.047

# Swerve direction
swerve_direction = "LEFT" if gyro_z_corrected > 0 else "RIGHT"

# Swerve intensity (how far above threshold)
swerve_intensity = abs(gyro_z_corrected) / 1.047  # 1.0 = at threshold
```

**Expected Behavior:**
- **Straight driving:** yaw rate < 0.3 rad/s (~15°/s)
- **Normal turn:** yaw rate 0.3 - 0.8 rad/s (15-45°/s)
- **Swerving:** yaw rate > 1.047 rad/s (>60°/s)

**Validation Test:**
1. Drive straight: verify no swerve detection
2. Make gradual turn: verify no swerve (below threshold)
3. Make sharp turn: verify swerve detected
4. Check direction matches turn direction

**Success Criteria:**
- Swerving detected during sharp turns (>60°/s)
- Direction classification correct (left/right)
- No false positives during normal driving (<5% false positive rate)

---

### 6.4 Rollover Detection (Rapid Roll Rate)

**Definition:** Detect potential rollover via rapid roll rate (>90°/s).

**How to Detect:**
```python
# Rollover: roll rate >90°/s = 1.571 rad/s
gyro_x_corrected = gyro_x - bias_x  # Bias-corrected roll rate
is_rollover_risk = abs(gyro_x_corrected) > 1.571

# Also check roll angle (vehicle on side)
roll, pitch, yaw = quaternion_to_euler(*state[6:10])
is_rolled_over = abs(roll) > np.radians(60)  # >60° roll = on side
```

**Expected Behavior:**
- **Normal driving:** roll rate < 0.3 rad/s, roll angle < 15°
- **Rollover event:** roll rate > 1.571 rad/s AND/OR roll angle > 60°

**Success Criteria:**
- Detects rapid roll rate (>90°/s) if it occurs
- Detects sustained roll angle (>60° for >2 seconds)
- No false positives from phone being picked up

---

### 6.5 False Positive Rate (Specificity)

**Definition:** How often does EKF falsely detect incidents during normal driving?

**How to Calculate:**
```python
# During normal driving test (no incidents)
total_samples = len(samples)
false_positive_samples = sum([1 for s in samples if is_incident(s) and not is_true_incident(s)])

false_positive_rate = false_positive_samples / total_samples * 100
```

**Expected Range:**
- **False positive rate:** <5% of samples
- **False incidents per hour:** <3 (at 50 Hz sampling)

**Validation Test:**
1. Drive normally for 30 minutes (no hard braking, no swerving)
2. Count incident detections
3. Verify <5% false positive rate

**Success Criteria:**
- False positive rate < 5%
- No false incidents during steady highway driving

---

## 7. ERROR REDUCTION METRICS

**Goal:** Quantify how much EKF reduces error vs baseline.

### 7.1 GPS Position Innovation Magnitude

**Definition:** Difference between GPS measurement and EKF position prediction.

**How to Calculate:**
```python
# In update_gps method
z = np.array([x_gps, y_gps])  # GPS measurement (converted to meters)
z_pred = H @ state  # Predicted GPS from state
innovation = z - z_pred
innovation_magnitude = np.linalg.norm(innovation)
```

**Expected Range:**
- **Initial (no data):** >10 m (high uncertainty)
- **After convergence:** <5 m (GPS accuracy limited)
- **During steady motion:** <2 m

**Success Criteria:**
- Innovation magnitude decreases over first minute
- Steady-state innovation < 5 m (limited by GPS accuracy)
- Innovation doesn't grow over time (no divergence)

---

### 7.2 State Covariance Trace (Uncertainty)

**Definition:** Sum of diagonal elements of covariance matrix (total uncertainty).

**How to Calculate:**
```python
covariance_trace = np.trace(P)  # Sum of diagonal: P[0,0] + P[1,1] + ... + P[12,12]
```

**Expected Range:**
- **Initial:** ~13,000 (high uncertainty, P initialized to 1000*I)
- **After GPS fix:** <1,000 (position uncertainty reduced)
- **Converged:** <100 (all states well-estimated)

**Warning Thresholds:**
- Trace increasing over time → Filter diverging
- Trace = 0 → Numerical underflow (covariance collapsed)

**Success Criteria:**
- Covariance trace decreases over first 2 minutes
- Steady-state trace < 100
- Trace remains positive (no underflow)

---

### 7.3 GPS Heading Convergence Error

**Definition:** How long until EKF heading matches GPS bearing?

**How to Calculate:**
```python
# For each GPS sample
gps_bearing = gps_data['bearing']  # degrees
roll, pitch, yaw = quaternion_to_euler(*state[6:10])
ekf_heading = np.degrees(yaw) % 360

heading_error = abs(gps_bearing - ekf_heading)
if heading_error > 180:
    heading_error = 360 - heading_error

# Record time when error first drops below 15°
if heading_error < 15 and convergence_time is None:
    convergence_time = current_time - start_time
```

**Expected Range:**
- **Convergence time:** <60 seconds
- **Steady-state error:** <15°

**Success Criteria:**
- Heading error < 15° within 60 seconds of motion
- Heading error decreases over time (not oscillating)

---

### 7.4 Velocity Estimation Error (vs GPS Speed)

**Definition:** Difference between EKF velocity and GPS-reported speed.

**How to Calculate:**
```python
ekf_velocity = np.sqrt(state[2]**2 + state[3]**2)  # sqrt(vx² + vy²)
gps_velocity = gps_data['speed']  # m/s

velocity_error = abs(ekf_velocity - gps_velocity)
velocity_error_pct = velocity_error / max(gps_velocity, 0.1) * 100
```

**Expected Range:**
- **Velocity error:** <1 m/s (~3.6 km/h)
- **Velocity error %:** <10% during steady motion

**Success Criteria:**
- Velocity error < 1 m/s after convergence
- Velocity error doesn't grow over time

---

## 8. RUNTIME PERFORMANCE METRICS

**Goal:** Ensure filter is computationally efficient and numerically stable.

### 8.1 Update Time per Sample

**Definition:** How long does each filter update take?

**How to Calculate:**
```python
import time

# GPS update
start = time.perf_counter()
ekf.update_gps(lat, lon, speed, accuracy)
gps_update_time = time.perf_counter() - start

# Accel update
start = time.perf_counter()
ekf.update_accelerometer(accel_magnitude)
accel_update_time = time.perf_counter() - start

# Gyro update
start = time.perf_counter()
ekf.update_gyroscope(gyro_x, gyro_y, gyro_z)
gyro_update_time = time.perf_counter() - start
```

**Expected Range:**
- **GPS update:** <1 ms (rare, 1 Hz)
- **Accel update:** <0.5 ms (50 Hz rate)
- **Gyro update:** <1 ms (20 Hz rate)

**Warning Thresholds:**
- Any update > 5 ms → Performance problem
- Updates getting slower over time → Memory leak

**Success Criteria:**
- All updates < 2 ms on average
- 99th percentile < 5 ms
- No performance degradation over 1 hour test

---

### 8.2 Memory Growth

**Definition:** Does filter memory usage grow unbounded?

**How to Calculate:**
```python
import psutil

process = psutil.Process()
mem_info = process.memory_info()
memory_mb = mem_info.rss / 1024 / 1024  # RSS in MB
```

**Expected Range:**
- **Initial:** ~50 MB (baseline + filter state)
- **After 1 hour:** <200 MB (with data logging)
- **Growth rate:** <1 MB per minute

**Warning Thresholds:**
- Memory > 500 MB → Data structure leak
- Growth rate > 5 MB per minute → Unbounded accumulation

**Success Criteria:**
- Memory stays bounded (<200 MB for 1 hour test)
- Memory plateaus after initial ramp-up (data buffers fill)

---

### 8.3 Numerical Stability (NaN/Inf Detection)

**Definition:** Check for numerical errors in state or covariance.

**How to Calculate:**
```python
# After each update
has_nan = np.any(np.isnan(state)) or np.any(np.isnan(P))
has_inf = np.any(np.isinf(state)) or np.any(np.isinf(P))

if has_nan or has_inf:
    log_error("NUMERICAL INSTABILITY DETECTED")
    # Log state, covariance, and recent measurements
```

**Expected:**
- **NaN/Inf occurrences:** ZERO (should never happen)

**Success Criteria:**
- No NaN or Inf values in state or covariance throughout entire test
- If NaN/Inf detected → CRITICAL BUG

---

### 8.4 Kalman Gain Stability

**Definition:** Kalman gain should decrease as filter converges (confidence increases).

**How to Calculate:**
```python
# In update methods, after computing Kalman gain K
kalman_gain_norm = np.linalg.norm(K)

kalman_gain_history.append({
    'timestamp': time.time() - start_time,
    'gain_norm': kalman_gain_norm,
    'update_type': 'gps' or 'accel' or 'gyro'
})
```

**Expected Range:**
- **Initial:** High gain (>0.5) - filter trusts measurements more
- **Converged:** Lower gain (<0.1) - filter trusts state estimate more

**Success Criteria:**
- Kalman gain decreases over first 2 minutes
- Gain stabilizes (not oscillating)
- Gain never goes negative (would be nonsensical)

---

## 9. SUMMARY METRICS DASHBOARD

### 9.1 Real-Time Display Format

```
[MM:SS] 13D GYRO-EKF VALIDATION METRICS
═══════════════════════════════════════════════════════════════════════════════

BIAS CONVERGENCE
  Bias Magnitude:        0.0123 rad/s  [Target: 0.001-0.05]  ✓
  Convergence Time:      23.5 sec      [Target: <30s]        ✓
  Bias Stability (σ):    0.0018 rad/s  [Target: <0.005]      ✓

QUATERNION HEALTH
  Norm:                  1.0002        [Target: 1.0±0.001]   ✓
  Norm Error:            0.0002        [Target: <0.001]      ✓
  Euler Angles:          r=2.3° p=-1.5° y=87.4°              ✓

ORIENTATION TRACKING
  GPS Heading:           87°
  EKF Heading:           87°
  Heading Error:         0°            [Target: <15°]        ✓

GYRO SIGNAL QUALITY
  Stationary Residual:   0.0082 rad/s  [Target: <0.01]       ✓
  Innovation Magnitude:  0.0156 rad/s  [Target: <0.02]       ✓

FILTER ACCURACY
  GPS Distance:          1234.5 m
  EKF Distance:          1230.2 m      [Error: 0.35%]        ✓
  Velocity Error:        0.12 m/s      [Target: <1.0]        ✓

INCIDENT DETECTION
  Swerving Confidence:   0.87          [Target: >0.8]        ✓
  Pitch Angle (braking): -0.5°         [Monitoring]
  Roll Angle (impacts):  2.3°          [Monitoring]

PERFORMANCE
  Update Time (avg):     0.45 ms       [Target: <2ms]        ✓
  Memory Usage:          87.3 MB       [Target: <200MB]      ✓
  Covariance Trace:      42.8          [Decreasing]          ✓

STATUS: ALL SYSTEMS NOMINAL ✓
```

### 9.2 Post-Test Summary Report

After test completes, generate comprehensive report:

```python
def generate_validation_report(metrics_history):
    """
    Generate comprehensive validation report from collected metrics.

    Args:
        metrics_history: Dictionary containing all metric time series

    Returns:
        Formatted report string with pass/fail status for each metric category
    """

    report = []
    report.append("=" * 100)
    report.append("13D GYRO-EKF VALIDATION REPORT")
    report.append("=" * 100)

    # 1. BIAS CONVERGENCE
    report.append("\n1. BIAS CONVERGENCE METRICS")
    report.append("-" * 100)

    bias_magnitude = metrics_history['bias_magnitude']
    convergence_time = compute_convergence_time(bias_magnitude)
    final_bias = np.mean(bias_magnitude[-20:])
    bias_stability = np.std(bias_magnitude[-20:])

    report.append(f"  Final Bias Magnitude:      {final_bias:.6f} rad/s  [Target: 0.001-0.05]")
    report.append(f"  Convergence Time:          {convergence_time:.1f} sec     [Target: <30s]")
    report.append(f"  Bias Stability (last 20s): {bias_stability:.6f} rad/s [Target: <0.005]")

    bias_pass = (0.001 < final_bias < 0.05 and
                 convergence_time < 30 and
                 bias_stability < 0.005)
    report.append(f"  Status: {'✓ PASS' if bias_pass else '✗ FAIL'}")

    # 2. QUATERNION HEALTH
    report.append("\n2. QUATERNION HEALTH METRICS")
    report.append("-" * 100)

    quat_norms = metrics_history['quaternion_norm']
    max_norm_error = max([abs(n - 1.0) for n in quat_norms])
    mean_norm_error = np.mean([abs(n - 1.0) for n in quat_norms])

    report.append(f"  Mean Norm Error:    {mean_norm_error:.6f}  [Target: <0.001]")
    report.append(f"  Max Norm Error:     {max_norm_error:.6f}   [Target: <0.01]")

    quat_pass = mean_norm_error < 0.001 and max_norm_error < 0.01
    report.append(f"  Status: {'✓ PASS' if quat_pass else '✗ FAIL'}")

    # 3. ORIENTATION TRACKING
    report.append("\n3. ORIENTATION TRACKING METRICS")
    report.append("-" * 100)

    heading_errors = metrics_history['heading_error']
    heading_convergence = compute_convergence_time(heading_errors, target=15.0, decreasing=True)
    steady_heading_error = np.mean([e for e in heading_errors if metrics_history['timestamp'][-len(heading_errors):] > 60])

    report.append(f"  Heading Convergence Time:  {heading_convergence:.1f} sec  [Target: <60s]")
    report.append(f"  Steady-State Heading Error: {steady_heading_error:.1f}°     [Target: <15°]")

    heading_pass = heading_convergence < 60 and steady_heading_error < 15
    report.append(f"  Status: {'✓ PASS' if heading_pass else '✗ FAIL'}")

    # 4. FILTER ACCURACY
    report.append("\n4. FILTER ACCURACY METRICS")
    report.append("-" * 100)

    gps_distance = metrics_history['gps_ground_truth_distance']
    ekf_distance = metrics_history['ekf_distance']
    distance_error_pct = abs(ekf_distance - gps_distance) / gps_distance * 100

    velocity_errors = metrics_history['velocity_error']
    mean_velocity_error = np.mean(velocity_errors)

    report.append(f"  GPS Ground Truth Distance:  {gps_distance:.2f} m")
    report.append(f"  EKF Estimated Distance:     {ekf_distance:.2f} m  [Error: {distance_error_pct:.2f}%]")
    report.append(f"  Mean Velocity Error:        {mean_velocity_error:.3f} m/s  [Target: <1.0]")

    accuracy_pass = distance_error_pct < 10 and mean_velocity_error < 1.0
    report.append(f"  Status: {'✓ PASS' if accuracy_pass else '✗ FAIL'}")

    # 5. INCIDENT DETECTION
    report.append("\n5. INCIDENT DETECTION METRICS")
    report.append("-" * 100)

    swerve_confidence = metrics_history.get('swerve_confidence_mean', 0)
    false_positive_rate = metrics_history.get('false_positive_rate', 0)

    report.append(f"  Swerving Detection Confidence:  {swerve_confidence:.2f}  [Target: >0.8]")
    report.append(f"  False Positive Rate:            {false_positive_rate:.2f}%   [Target: <5%]")

    incident_pass = swerve_confidence > 0.8 and false_positive_rate < 5
    report.append(f"  Status: {'✓ PASS' if incident_pass else '✗ FAIL'}")

    # 6. PERFORMANCE
    report.append("\n6. RUNTIME PERFORMANCE METRICS")
    report.append("-" * 100)

    update_times = metrics_history['update_times']
    mean_update_time = np.mean(update_times) * 1000  # Convert to ms
    p99_update_time = np.percentile(update_times, 99) * 1000

    memory = metrics_history['memory_mb']
    peak_memory = max(memory)

    report.append(f"  Mean Update Time:     {mean_update_time:.3f} ms  [Target: <2ms]")
    report.append(f"  99th Percentile Time: {p99_update_time:.3f} ms   [Target: <5ms]")
    report.append(f"  Peak Memory Usage:    {peak_memory:.1f} MB      [Target: <200MB]")

    perf_pass = mean_update_time < 2 and p99_update_time < 5 and peak_memory < 200
    report.append(f"  Status: {'✓ PASS' if perf_pass else '✗ FAIL'}")

    # OVERALL RESULT
    report.append("\n" + "=" * 100)
    all_pass = bias_pass and quat_pass and heading_pass and accuracy_pass and incident_pass and perf_pass

    if all_pass:
        report.append("OVERALL RESULT: ✓ ALL METRICS PASS - 13D GYRO-EKF VALIDATED")
        report.append("\nRECOMMENDATION: READY FOR PRODUCTION USE")
    else:
        report.append("OVERALL RESULT: ✗ SOME METRICS FAILED - TUNING NEEDED")
        report.append("\nRECOMMENDATION: Review failed metrics and adjust filter parameters")

    report.append("=" * 100)

    return "\n".join(report)
```

---

## 10. IMPLEMENTATION CHECKLIST

### Phase 1: Basic Metrics (Core Validation)
- [ ] Bias magnitude tracking
- [ ] Quaternion norm check
- [ ] Bias convergence time
- [ ] Stationary gyro residual
- [ ] GPS heading vs EKF heading comparison

### Phase 2: Orientation Metrics (Rotation Tracking)
- [ ] Quaternion to Euler conversion
- [ ] Static rotation tests (90° yaw, 45° pitch, 90° roll)
- [ ] Quaternion rate vs gyro magnitude correlation
- [ ] Euler angle consistency checks

### Phase 3: Incident Detection Metrics (Real-World Goal)
- [ ] Swerving detection (yaw rate >60°/s)
- [ ] Hard braking pitch angle detection
- [ ] Impact direction classification (roll/pitch analysis)
- [ ] Rollover detection (roll rate >90°/s)
- [ ] False positive rate measurement

### Phase 4: Error Reduction Metrics (Filter Quality)
- [ ] Innovation magnitude tracking
- [ ] Covariance trace monitoring
- [ ] Position accuracy vs GPS ground truth
- [ ] Velocity smoothness comparison
- [ ] Heading convergence error

### Phase 5: Performance Metrics (Computational Efficiency)
- [ ] Update time per sample
- [ ] Memory growth monitoring
- [ ] NaN/Inf detection
- [ ] Kalman gain stability

### Phase 6: Real-Time Dashboard & Reporting
- [ ] Real-time metrics display (every 1 second)
- [ ] Status log (every 30 seconds)
- [ ] Post-test summary report
- [ ] Metric history JSON export

---

## 11. METRIC COLLECTION CODE STRUCTURE

### 11.1 MetricsCollector Class

```python
class GyroEKFMetricsCollector:
    """
    Collects and analyzes validation metrics for 13D Gyro-EKF.

    Usage:
        metrics = GyroEKFMetricsCollector()

        # In filter update loop
        metrics.record_bias(state[10:13])
        metrics.record_quaternion(state[6:10])
        metrics.record_gyro_measurement(gyro_x, gyro_y, gyro_z)
        metrics.record_innovation(innovation_magnitude)

        # Display real-time metrics
        if time.time() - last_display > 1.0:
            metrics.display_summary()

        # Generate final report
        report = metrics.generate_report()
        print(report)
    """

    def __init__(self):
        self.start_time = time.time()

        # Bias tracking
        self.bias_history = deque(maxlen=10000)

        # Quaternion tracking
        self.quaternion_history = deque(maxlen=10000)

        # Gyro measurements
        self.gyro_history = deque(maxlen=10000)

        # Innovation (measurement residuals)
        self.innovation_history = deque(maxlen=10000)

        # Performance
        self.update_times = deque(maxlen=10000)
        self.memory_samples = deque(maxlen=1000)

        # Incident detection
        self.incident_detections = []

        # Process handle for memory monitoring
        self.process = psutil.Process()

    def record_bias(self, bias_vector):
        """Record gyro bias estimate [bx, by, bz]"""
        self.bias_history.append({
            'timestamp': time.time() - self.start_time,
            'bx': bias_vector[0],
            'by': bias_vector[1],
            'bz': bias_vector[2],
            'magnitude': np.linalg.norm(bias_vector)
        })

    def record_quaternion(self, quaternion):
        """Record quaternion state [q0, q1, q2, q3]"""
        q_norm = np.linalg.norm(quaternion)
        roll, pitch, yaw = quaternion_to_euler(*quaternion)

        self.quaternion_history.append({
            'timestamp': time.time() - self.start_time,
            'q0': quaternion[0],
            'q1': quaternion[1],
            'q2': quaternion[2],
            'q3': quaternion[3],
            'norm': q_norm,
            'norm_error': abs(q_norm - 1.0),
            'roll': roll,
            'pitch': pitch,
            'yaw': yaw
        })

    def record_gyro_measurement(self, gyro_x, gyro_y, gyro_z, bias_x=0, bias_y=0, bias_z=0):
        """Record gyro measurement and bias-corrected residual"""
        residual_x = gyro_x - bias_x
        residual_y = gyro_y - bias_y
        residual_z = gyro_z - bias_z
        residual_magnitude = np.sqrt(residual_x**2 + residual_y**2 + residual_z**2)

        self.gyro_history.append({
            'timestamp': time.time() - self.start_time,
            'gyro_x': gyro_x,
            'gyro_y': gyro_y,
            'gyro_z': gyro_z,
            'residual_x': residual_x,
            'residual_y': residual_y,
            'residual_z': residual_z,
            'residual_magnitude': residual_magnitude
        })

    def record_innovation(self, innovation_magnitude, update_type='gyro'):
        """Record innovation (measurement residual) magnitude"""
        self.innovation_history.append({
            'timestamp': time.time() - self.start_time,
            'magnitude': innovation_magnitude,
            'type': update_type
        })

    def record_update_time(self, update_time_seconds):
        """Record filter update time"""
        self.update_times.append(update_time_seconds)

    def record_memory(self):
        """Record current memory usage"""
        mem_info = self.process.memory_info()
        memory_mb = mem_info.rss / 1024 / 1024
        self.memory_samples.append({
            'timestamp': time.time() - self.start_time,
            'memory_mb': memory_mb
        })

    def compute_bias_convergence_time(self):
        """Compute time for bias to reach 90% of final value"""
        if len(self.bias_history) < 20:
            return None

        final_bias = np.mean([h['magnitude'] for h in list(self.bias_history)[-20:]])
        target = 0.9 * final_bias

        for sample in self.bias_history:
            if sample['magnitude'] >= target:
                return sample['timestamp']
        return None

    def display_summary(self):
        """Display real-time metrics summary"""
        if not self.bias_history or not self.quaternion_history:
            return

        elapsed = time.time() - self.start_time
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        # Latest values
        latest_bias = self.bias_history[-1]['magnitude']
        latest_quat = self.quaternion_history[-1]

        # Compute metrics
        convergence_time = self.compute_bias_convergence_time()

        print(f"\n[{mins:02d}:{secs:02d}] 13D GYRO-EKF METRICS")
        print("=" * 80)
        print(f"  Bias Magnitude:     {latest_bias:.6f} rad/s")
        if convergence_time:
            print(f"  Convergence Time:   {convergence_time:.1f} sec")
        print(f"  Quaternion Norm:    {latest_quat['norm']:.6f}  (error: {latest_quat['norm_error']:.6f})")
        print(f"  Euler Angles:       roll={np.degrees(latest_quat['roll']):.1f}° "
              f"pitch={np.degrees(latest_quat['pitch']):.1f}° "
              f"yaw={np.degrees(latest_quat['yaw']):.1f}°")

        if self.update_times:
            mean_update_time = np.mean(list(self.update_times)) * 1000
            print(f"  Update Time (avg):  {mean_update_time:.3f} ms")

        if self.memory_samples:
            latest_memory = self.memory_samples[-1]['memory_mb']
            print(f"  Memory Usage:       {latest_memory:.1f} MB")

        print("=" * 80)

    def generate_report(self):
        """Generate comprehensive validation report (see Section 9.2)"""
        # Implementation as shown in Section 9.2
        pass
```

---

## 12. TESTING PROTOCOL

### 12.1 Stationary Test (Bias Learning)

**Duration:** 5 minutes
**Purpose:** Verify bias convergence and quaternion stability

**Procedure:**
1. Place phone flat on stable surface (table, not hand)
2. Run filter with gyro enabled
3. Do not move phone for entire test
4. Record all metrics

**Expected Results:**
- Bias magnitude converges to 0.001-0.05 rad/s within 30s
- Quaternion norm stays 1.0 ± 0.001
- Bias-corrected gyro residual < 0.01 rad/s
- Euler angles stay constant (±2°)

### 12.2 Rotation Test (Orientation Tracking)

**Duration:** 2 minutes
**Purpose:** Verify rotation detection accuracy

**Procedure:**
1. Start with phone flat, record initial orientation
2. Wait 10 seconds for bias convergence
3. Rotate phone 90° clockwise (yaw), wait 5 seconds
4. Measure yaw angle change
5. Return to original position, wait 5 seconds
6. Rotate phone 45° forward (pitch), wait 5 seconds
7. Measure pitch angle change
8. Repeat for roll rotation

**Expected Results:**
- 90° yaw rotation detected as 90° ± 5°
- 45° pitch rotation detected as 45° ± 5°
- 90° roll rotation detected as 90° ± 5°
- Angles return to baseline after returning to original position

### 12.3 Driving Test (Incident Detection)

**Duration:** 10-30 minutes
**Purpose:** Verify real-world incident detection performance

**Procedure:**
1. Mount phone in vehicle (secure mount, no hand-holding)
2. Start filter with GPS + accel + gyro enabled
3. Drive normally for 5 minutes (baseline)
4. Perform controlled maneuvers:
   - Hard braking (1 event)
   - Sharp turn (1 event per direction)
   - U-turn (1 event)
5. Drive normally for 5 more minutes

**Expected Results:**
- Hard braking: pitch angle drops >5°, detected as hard braking event
- Sharp turn: yaw rate >60°/s, detected as swerving event
- Normal driving: <5% false positive rate
- GPS heading converges to EKF heading within 60s

---

## 13. PASS/FAIL CRITERIA SUMMARY

| Metric Category | Key Criteria | Pass Threshold |
|-----------------|--------------|----------------|
| **Bias Convergence** | Bias magnitude converges | >0.001 rad/s after 30s |
| | Convergence time | <30 seconds |
| | Bias stability | σ < 0.005 rad/s |
| **Quaternion Health** | Norm error | <0.001 (0.1%) |
| | Norm stability | No jumps >0.01 |
| **Orientation Tracking** | Rotation accuracy | ±5° for 90° rotations |
| | Heading convergence | <60 seconds |
| | Heading error | <15° steady-state |
| **Gyro Signal** | Stationary residual | <0.01 rad/s |
| | Innovation magnitude | <0.02 rad/s after convergence |
| **Filter Accuracy** | Distance error | <10% vs GPS |
| | Velocity error | <1 m/s |
| **Incident Detection** | Swerving confidence | >0.8 (80%) |
| | False positive rate | <5% |
| | Hard braking pitch detection | >5° nose-down |
| **Performance** | Update time | <2 ms mean, <5 ms p99 |
| | Memory usage | <200 MB for 1 hour |
| | Numerical stability | Zero NaN/Inf |

---

## 14. TROUBLESHOOTING GUIDE

### Problem: Bias stuck at zero
**Symptoms:** Bias magnitude < 0.0001 rad/s after 60s
**Possible Causes:**
- Gyro measurements not reaching filter (check data pipeline)
- Bias update disabled (check measurement Jacobian H)
- Process noise too low (bias can't change)

**Fixes:**
- Verify gyro data reaches `update_gyroscope()` method
- Check Jacobian: `H[0:3, 10:13]` should be identity
- Increase bias process noise: `Q[10:13, 10:13] = 0.01**2` (try 0.01 instead of 0.001)

### Problem: Quaternion norm diverging
**Symptoms:** Norm error > 0.01, growing over time
**Possible Causes:**
- Quaternion not normalized after update
- Integration error accumulating
- Covariance becoming singular

**Fixes:**
- Add explicit normalization: `state[6:10] = quaternion_normalize(state[6:10])`
- Check dt is correct (not too large, <0.1s)
- Use Joseph form covariance update

### Problem: Heading never converges to GPS
**Symptoms:** Heading error > 30° after 2 minutes
**Possible Causes:**
- Bias not learned (gyro measurements wrong)
- Quaternion integration backward (sign error)
- GPS bearing in wrong units (degrees vs radians)

**Fixes:**
- Check bias is learning (magnitude > 0.001 rad/s)
- Verify quaternion multiplication order: `q_new = q + 0.5*dt*q*[0,ω]`
- Verify GPS bearing is in degrees, convert for comparison

### Problem: High false positive rate (>10%)
**Symptoms:** Incidents detected during normal driving
**Possible Causes:**
- Thresholds too low
- Phone not securely mounted (vibration noise)
- Bias not converged yet

**Fixes:**
- Increase thresholds: swerving 60°/s → 80°/s, braking 0.8g → 1.0g
- Use secure vehicle mount (not hand-held)
- Wait 60s after start before enabling incident detection

---

## 15. REFERENCES & RESOURCES

### Key Equations

**Quaternion Normalization:**
```
q_norm = sqrt(q0² + q1² + q2² + q3²)
q_normalized = [q0/q_norm, q1/q_norm, q2/q_norm, q3/q_norm]
```

**Quaternion Kinematics:**
```
dq/dt = 0.5 * q ⊗ [0, ωx, ωy, ωz]
q(t+dt) = q(t) + dq/dt * dt
```

**Quaternion to Euler:**
```
roll  = atan2(2*(q0*q1 + q2*q3), 1 - 2*(q1² + q2²))
pitch = asin(2*(q0*q2 - q3*q1))
yaw   = atan2(2*(q0*q3 + q1*q2), 1 - 2*(q2² + q3²))
```

**Bias-Corrected Gyro:**
```
ω_true = ω_measured - bias
```

### Typical Smartphone Gyro Specs
- **Noise density:** 0.01 - 0.05 rad/s (stationary std dev)
- **Bias stability:** 0.001 - 0.02 rad/s (drift rate)
- **Temperature sensitivity:** 0.01 rad/s per °C
- **Sampling rate:** 100-200 Hz (but Termux limited to ~20 Hz)

### Incident Detection Thresholds (from CLAUDE.md)
- **Hard braking:** >0.8g deceleration (7.85 m/s²)
- **Swerving:** >60°/s yaw rate (1.047 rad/s)
- **Impact:** >1.5g acceleration (14.7 m/s²)
- **Rollover:** >90°/s roll rate or >60° roll angle

---

**END OF VALIDATION METRICS FRAMEWORK**

This comprehensive framework provides everything needed to validate the 13D gyro-EKF is working correctly and improving incident detection accuracy. Implement metrics progressively (Phase 1-6 checklist) and use the MetricsCollector class for consistent data collection.
