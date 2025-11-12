# Filter Architecture Refactor Plan

**Status:** 📋 PLANNED (Implementation scheduled for tomorrow)
**Created:** Nov 12, 2025

---

## Problem Statement

**Current Architecture (Synchronous):**
- Filters run synchronously in data collection loops
- `_accel_loop` (line 721-789): Gets accel → calibrate → update EKF/Complementary/ES-EKF → append results
- `_gps_loop` (line 605-719): Gets GPS → update all 3 filters
- `_gyro_loop` (line 838-941): Gets gyro → update EKF with gyro

**Critical Issue:**
If any filter hangs (e.g., ES-EKF at sample #6), ALL data collection stops.

**User Vision:**
> "we should be getting that base data from gps, accel, gyro, then the filters do their independent things"

---

## Proposed Architecture (Decoupled)

### Data Flow
```
Sensor Daemons → Raw Queues → Collection Loops → Filter Queues → Filter Threads → Results
     ↓              ↓              ↓                    ↓               ↓              ↓
GPS daemon    gps_raw_queue   _gps_loop         ekf_gps_queue     ekf_thread    ekf_results
Accel daemon  accel_raw_queue _accel_loop       comp_gps_queue    comp_thread   comp_results
Gyro daemon   gyro_raw_queue  _gyro_loop        es_ekf_gps_queue  es_ekf_thread es_results
```

### Design Principles
1. Raw data collection NEVER blocks (even if all filters crash)
2. Each filter runs independently in its own thread
3. Filters consume from shared raw data queues
4. Failed filters don't impact other filters or collection

---

## Implementation Plan

### Phase 1: Add Raw Data Queues

**Location:** After filter initialization (line 296), before data storage deques (line 310)

**New Queues:**
```python
# Raw sensor data queues (producers: sensor daemons, consumers: collection loops)
self.accel_raw_queue = Queue(maxsize=1000)  # ~50s buffer @ 20Hz
self.gps_raw_queue = Queue(maxsize=100)     # ~100s buffer @ 1Hz
self.gyro_raw_queue = Queue(maxsize=1000)   # ~50s buffer @ 20Hz

# Per-filter input queues (producers: collection loops, consumers: filter threads)
self.ekf_accel_queue = Queue(maxsize=500)
self.ekf_gps_queue = Queue(maxsize=50)
self.ekf_gyro_queue = Queue(maxsize=500)

self.comp_accel_queue = Queue(maxsize=500)
self.comp_gps_queue = Queue(maxsize=50)

self.es_ekf_accel_queue = Queue(maxsize=500)
self.es_ekf_gps_queue = Queue(maxsize=50)
self.es_ekf_gyro_queue = Queue(maxsize=500)
```

**Total:** 12 queues (~20MB additional memory)

---

### Phase 2: Modify Collection Loops (Decouple from Filters)

**Current Behavior:** Collection loops read from sensor daemons AND update filters
**New Behavior:** Collection loops ONLY queue raw data (no filter calls)

#### A. `_gps_loop` (lines 605-719)

**Remove:**
- Lines 621-626: Filter update calls (EKF, Complementary, ES-EKF)

**Add:**
```python
# Package GPS data with timestamp
gps_packet = {
    'timestamp': timestamp,
    'latitude': gps['latitude'],
    'longitude': gps['longitude'],
    'speed': gps['speed'],
    'accuracy': gps['accuracy'],
    'provider': gps.get('provider', 'gps')
}

# Distribute to ALL filter queues (non-blocking)
try:
    self.ekf_gps_queue.put_nowait(gps_packet)
except:
    pass  # Queue full, drop oldest
try:
    self.comp_gps_queue.put_nowait(gps_packet)
except:
    pass
try:
    self.es_ekf_gps_queue.put_nowait(gps_packet)
except:
    pass
```

#### B. `_accel_loop` (lines 721-789)

**Remove:**
- Lines 756-761: Filter update calls (EKF, Complementary, ES-EKF)
- Lines 762-788: Result list appending

**Add:**
```python
# Package accel data with timestamp
timestamp = time.time() - self.start_time
accel_packet = {
    'timestamp': timestamp,
    'magnitude': motion_magnitude
}

# Distribute to ALL filter queues (non-blocking)
try:
    self.ekf_accel_queue.put_nowait(accel_packet)
except:
    pass
try:
    self.comp_accel_queue.put_nowait(accel_packet)
except:
    pass
try:
    self.es_ekf_accel_queue.put_nowait(accel_packet)
except:
    pass
```

**Keep:**
- Gravity calibration (lines 735-754)
- Incident detection (lines 790-797)

#### C. `_gyro_loop` (lines 838-941)

**Remove:**
- Lines 893-896: EKF/ES-EKF gyro update calls
- Lines 897-933: Result storage and metrics

**Add:**
```python
# Package gyro data
timestamp = time.time() - self.start_time
gyro_packet = {
    'timestamp': timestamp,
    'gyro_x': gyro_x,
    'gyro_y': gyro_y,
    'gyro_z': gyro_z,
    'magnitude': magnitude
}

# Distribute to filter queues (only EKF and ES-EKF support gyro)
try:
    self.ekf_gyro_queue.put_nowait(gyro_packet)
except:
    pass
try:
    self.es_ekf_gyro_queue.put_nowait(gyro_packet)
except:
    pass
```

**Keep:**
- Incident detection (lines 868-886)

---

### Phase 3: Create Independent Filter Threads

**Location:** After `_gyro_loop` (line 941), before `_display_loop` (line 943)

#### A. `_ekf_filter_thread()`

```python
def _ekf_filter_thread(self):
    """Independent EKF filter processing thread"""
    print("[EKF_THREAD] Started", file=sys.stderr)
    samples_processed = {'accel': 0, 'gps': 0, 'gyro': 0}

    while not self.stop_event.is_set():
        try:
            # Process accel (high frequency - non-blocking check)
            try:
                accel_packet = self.ekf_accel_queue.get(timeout=0.01)
                v, d = self.ekf.update_accelerometer(accel_packet['magnitude'])
                samples_processed['accel'] += 1

                # Store result (thread-safe with lock)
                with self._save_lock:
                    self.accel_samples.append({
                        'timestamp': accel_packet['timestamp'],
                        'magnitude': accel_packet['magnitude'],
                        'ekf_velocity': v,
                        'ekf_distance': d
                    })
            except Empty:
                pass

            # Process GPS (low frequency)
            try:
                gps_packet = self.ekf_gps_queue.get(timeout=0.01)
                v, d = self.ekf.update_gps(
                    gps_packet['latitude'], gps_packet['longitude'],
                    gps_packet['speed'], gps_packet['accuracy']
                )
                samples_processed['gps'] += 1

                # Store trajectory
                if hasattr(self.ekf, 'get_position'):
                    try:
                        lat, lon, unc = self.ekf.get_position()
                        with self._save_lock:
                            self.ekf_trajectory.append({
                                'timestamp': gps_packet['timestamp'],
                                'lat': lat,
                                'lon': lon,
                                'uncertainty_m': unc,
                                'velocity': v
                            })
                    except:
                        pass

                # Update GPS samples with EKF results
                with self._save_lock:
                    if self.gps_samples:
                        self.gps_samples[-1]['ekf_velocity'] = v
                        self.gps_samples[-1]['ekf_distance'] = d

            except Empty:
                pass

            # Process gyro (if enabled)
            if self.enable_gyro:
                try:
                    gyro_packet = self.ekf_gyro_queue.get(timeout=0.01)
                    v, d = self.ekf.update_gyroscope(
                        gyro_packet['gyro_x'], gyro_packet['gyro_y'], gyro_packet['gyro_z']
                    )
                    samples_processed['gyro'] += 1

                    # Store gyro sample with EKF results
                    ekf_state = self.ekf.get_state()
                    with self._save_lock:
                        self.gyro_samples.append({
                            'timestamp': gyro_packet['timestamp'],
                            'gyro_x': gyro_packet['gyro_x'],
                            'gyro_y': gyro_packet['gyro_y'],
                            'gyro_z': gyro_packet['gyro_z'],
                            'magnitude': gyro_packet['magnitude'],
                            'ekf_velocity': v,
                            'ekf_distance': d,
                            'ekf_heading': ekf_state.get('heading_deg')
                        })

                    # Update metrics
                    if self.metrics:
                        gps_heading = None
                        with self._save_lock:
                            if self.gps_samples:
                                latest_gps = self.gps_samples[-1]
                                gps_heading = latest_gps.get('bearing', latest_gps.get('heading'))

                        accel_magnitude = 0
                        with self._save_lock:
                            if self.accel_samples:
                                accel_magnitude = self.accel_samples[-1]['magnitude']

                        self.metrics.update(
                            ekf_state=ekf_state,
                            gyro_measurement=[gyro_packet['gyro_x'], gyro_packet['gyro_y'], gyro_packet['gyro_z']],
                            gps_heading=gps_heading,
                            accel_magnitude=accel_magnitude
                        )
                except Empty:
                    pass

            # Brief sleep to avoid CPU spinning
            time.sleep(0.001)

        except Exception as e:
            print(f"ERROR in EKF filter thread: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            time.sleep(0.1)  # Backoff on error

    print(f"[EKF_THREAD] Exited after processing {samples_processed}", file=sys.stderr)
```

#### B. `_complementary_filter_thread()`

```python
def _complementary_filter_thread(self):
    """Independent Complementary filter processing thread"""
    print("[COMP_THREAD] Started", file=sys.stderr)
    samples_processed = {'accel': 0, 'gps': 0}

    while not self.stop_event.is_set():
        try:
            # Process accel
            try:
                accel_packet = self.comp_accel_queue.get(timeout=0.01)
                v, d = self.complementary.update_accelerometer(accel_packet['magnitude'])
                samples_processed['accel'] += 1

                # Update accel samples (find matching timestamp)
                with self._save_lock:
                    for sample in reversed(self.accel_samples):
                        if abs(sample['timestamp'] - accel_packet['timestamp']) < 0.01:
                            sample['comp_velocity'] = v
                            sample['comp_distance'] = d
                            break
            except Empty:
                pass

            # Process GPS
            try:
                gps_packet = self.comp_gps_queue.get(timeout=0.01)
                v, d = self.complementary.update_gps(
                    gps_packet['latitude'], gps_packet['longitude'],
                    gps_packet['speed'], gps_packet['accuracy']
                )
                samples_processed['gps'] += 1

                # Store trajectory
                if hasattr(self.complementary, 'get_position'):
                    try:
                        lat, lon, unc = self.complementary.get_position()
                        with self._save_lock:
                            self.comp_trajectory.append({
                                'timestamp': gps_packet['timestamp'],
                                'lat': lat,
                                'lon': lon,
                                'uncertainty_m': unc,
                                'velocity': v
                            })
                    except:
                        pass

                # Update GPS samples
                with self._save_lock:
                    if self.gps_samples:
                        self.gps_samples[-1]['comp_velocity'] = v
                        self.gps_samples[-1]['comp_distance'] = d

            except Empty:
                pass

            time.sleep(0.001)

        except Exception as e:
            print(f"ERROR in Complementary filter thread: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            time.sleep(0.1)

    print(f"[COMP_THREAD] Exited after processing {samples_processed}", file=sys.stderr)
```

#### C. `_es_ekf_filter_thread()`

```python
def _es_ekf_filter_thread(self):
    """Independent ES-EKF filter processing thread (EXPERIMENTAL - may hang)"""
    print("[ES_EKF_THREAD] Started", file=sys.stderr)
    samples_processed = {'accel': 0, 'gps': 0, 'gyro': 0}
    consecutive_failures = 0

    while not self.stop_event.is_set():
        try:
            # Process accel
            try:
                accel_packet = self.es_ekf_accel_queue.get(timeout=0.01)
                v, d = self.es_ekf.update_accelerometer(accel_packet['magnitude'])
                samples_processed['accel'] += 1
                consecutive_failures = 0

                # Update accel samples
                with self._save_lock:
                    for sample in reversed(self.accel_samples):
                        if abs(sample['timestamp'] - accel_packet['timestamp']) < 0.01:
                            sample['es_ekf_velocity'] = v
                            sample['es_ekf_distance'] = d
                            break
            except Empty:
                pass
            except Exception as e:
                # ES-EKF may hang - log but continue
                consecutive_failures += 1
                if consecutive_failures == 1:
                    print(f"⚠️  ES-EKF accel update failed at sample #{samples_processed['accel']}: {e}", file=sys.stderr)

                # After 10 consecutive failures, drain queue to prevent backup
                if consecutive_failures > 10:
                    try:
                        drained = 0
                        while True:
                            self.es_ekf_accel_queue.get_nowait()
                            drained += 1
                    except Empty:
                        print(f"  → Drained {drained} backed-up accel samples", file=sys.stderr)
                        consecutive_failures = 0

            # Process GPS
            try:
                gps_packet = self.es_ekf_gps_queue.get(timeout=0.01)
                v, d = self.es_ekf.update_gps(
                    gps_packet['latitude'], gps_packet['longitude'],
                    gps_packet['speed'], gps_packet['accuracy']
                )
                samples_processed['gps'] += 1

                # Store trajectory
                try:
                    lat, lon, unc = self.es_ekf.get_position()
                    with self._save_lock:
                        self.es_ekf_trajectory.append({
                            'timestamp': gps_packet['timestamp'],
                            'lat': lat,
                            'lon': lon,
                            'uncertainty_m': unc,
                            'velocity': v
                        })
                except:
                    pass

                # Update GPS samples
                with self._save_lock:
                    if self.gps_samples:
                        self.gps_samples[-1]['es_ekf_velocity'] = v
                        self.gps_samples[-1]['es_ekf_distance'] = d

            except Empty:
                pass
            except Exception as e:
                print(f"⚠️  ES-EKF GPS update failed: {e}", file=sys.stderr)

            # Process gyro
            if self.enable_gyro:
                try:
                    gyro_packet = self.es_ekf_gyro_queue.get(timeout=0.01)
                    v, d = self.es_ekf.update_gyroscope(
                        gyro_packet['gyro_x'], gyro_packet['gyro_y'], gyro_packet['gyro_z']
                    )
                    samples_processed['gyro'] += 1
                except Empty:
                    pass
                except Exception as e:
                    print(f"⚠️  ES-EKF gyro update failed: {e}", file=sys.stderr)

            time.sleep(0.001)

        except Exception as e:
            print(f"ERROR in ES-EKF filter thread (continuing): {e}", file=sys.stderr)
            time.sleep(0.1)

    print(f"[ES_EKF_THREAD] Exited after processing {samples_processed}", file=sys.stderr)
```

---

### Phase 4: Update `start()` Method

**Location:** Line 560-578 (thread launch section)

**Add filter thread launches:**
```python
# Start GPS collection thread
gps_thread = threading.Thread(target=self._gps_loop, daemon=True, name="GPSCollection")
gps_thread.start()

# Start accel collection thread
accel_thread = threading.Thread(target=self._accel_loop, daemon=True, name="AccelCollection")
accel_thread.start()

# Start gyro collection thread (if enabled)
if self.gyro_daemon:
    gyro_thread = threading.Thread(target=self._gyro_loop, daemon=True, name="GyroCollection")
    gyro_thread.start()

# Start filter processing threads (INDEPENDENT - NEW)
ekf_thread = threading.Thread(target=self._ekf_filter_thread, daemon=True, name="EKF_Filter")
ekf_thread.start()

comp_thread = threading.Thread(target=self._complementary_filter_thread, daemon=True, name="Comp_Filter")
comp_thread.start()

es_ekf_thread = threading.Thread(target=self._es_ekf_filter_thread, daemon=True, name="ES_EKF_Filter")
es_ekf_thread.start()

# Start health monitor thread
health_thread = threading.Thread(target=self._health_monitor_loop, daemon=True, name="HealthMonitor")
health_thread.start()

# Start display thread
display_thread = threading.Thread(target=self._display_loop, daemon=True, name="Display")
display_thread.start()
```

---

### Phase 5: Thread-Safe Result Storage

**Challenge:** Multiple filter threads write to shared data structures (`accel_samples`, `gps_samples`, `gyro_samples`)

**Solution:** Use existing `self._save_lock` around ALL deque writes

**Pattern:**
```python
with self._save_lock:
    self.accel_samples.append({...})
```

**Apply to:**
- All filter thread writes to `accel_samples`, `gps_samples`, `gyro_samples`
- All trajectory deque writes (`ekf_trajectory`, `comp_trajectory`, `es_ekf_trajectory`)

**GPS loop addition:** Add GPS sample creation in `_gps_loop` (currently only done in filter updates)

---

### Phase 6: Edge Case Handling

#### A. Filter Crash Recovery
- **Wrap all filter updates in try/except** ✓ (already in plan)
- **Log errors but continue processing** ✓
- **Drain queue on repeated failures** ✓ (ES-EKF thread has this)

#### B. Queue Overflow
- **Use `put_nowait()` with try/except** ✓ (drops old data if full)
- **Monitor queue sizes in `_display_loop`** (add warning if queues backing up)

**Add to `_display_loop`:**
```python
# Check queue health
queue_depths = {
    'ekf_accel': self.ekf_accel_queue.qsize(),
    'ekf_gps': self.ekf_gps_queue.qsize(),
    'comp_accel': self.comp_accel_queue.qsize(),
    'es_ekf_accel': self.es_ekf_accel_queue.qsize()
}

for name, depth in queue_depths.items():
    if depth > 400:  # 80% full warning
        print(f"⚠️  Queue {name} backing up: {depth} items", file=sys.stderr)
```

#### C. Shutdown Coordination
- **`stop_event.set()` triggers ALL threads to exit** ✓ (existing pattern)
- **No changes needed** ✓

#### D. Data Synchronization
- **GPS samples won't have all filter results immediately** (filters lag)
- **Display loop must handle missing fields gracefully:**

```python
# Safe access pattern
ekf_vel = gps_sample.get('ekf_velocity', 0)
comp_vel = gps_sample.get('comp_velocity', 0)
es_ekf_vel = gps_sample.get('es_ekf_velocity', 0)
```

---

## Benefits

1. **Resilience:** ES-EKF hang doesn't block accel/GPS collection
2. **Performance:** Filters run in parallel (multi-core utilization)
3. **Debugging:** Per-filter logs show which filter is slow/stuck
4. **Extensibility:** Easy to add new filters (just add queue + thread)
5. **Testing:** Can disable individual filters (skip thread launch)

---

## Risks & Mitigation

### Low Risk
- Collection loops simplified (less blocking) ✓
- Filter threads isolated (failures don't cascade) ✓
- Incident detection unchanged (runs in collection loops) ✓

### Medium Risk
- **Result storage synchronization** → Use locks around deque writes
- **Display metrics may show temporary inconsistencies** → Check for None values
- **Auto-save must handle incomplete filter results** → Use .get() with defaults

### High Risk
- **ES-EKF may still hang** → Thread will die, but collection continues ✓
- **Queue memory usage** → 12 queues vs 3 (~20MB increase, acceptable)

---

## Testing Strategy

### Unit Test
Individual filter thread processing with mock queues

### Integration Test
All filters running with simulated sensor data (inject known sequence)

### Stress Test
ES-EKF hang scenario - verify collection continues uninterrupted

### Long Run
30+ min test to validate stability, memory bounds, no queue buildup

### Validation Criteria
- ✓ Accel collection continuous even if ES-EKF hangs
- ✓ GPS collection continuous even if all filters crash
- ✓ Filter threads restart after crashes (optional enhancement)
- ✓ Memory stays bounded (92-132 MB with +20MB for queues)
- ✓ All 3 filters produce results (check final sample counts)

---

## Implementation Order (Tomorrow)

1. **Phase 1:** Add queues (5 min)
2. **Phase 2:** Modify collection loops (15 min)
3. **Phase 3:** Create filter threads (30 min)
4. **Phase 4:** Update start() (5 min)
5. **Phase 5:** Add locks to result writes (10 min)
6. **Phase 6:** Add queue monitoring (10 min)
7. **Testing:** Run 5-min test, verify all sensors + filters working (10 min)

**Total Estimate:** 85 minutes

---

## File Changes

**Primary File:** `motion_tracker_v2/test_ekf_vs_complementary.py`

**Line Ranges:**
- Lines 285-365: Add queue initialization (Phase 1)
- Lines 605-719: Modify `_gps_loop` (Phase 2)
- Lines 721-789: Modify `_accel_loop` (Phase 2)
- Lines 838-941: Modify `_gyro_loop` (Phase 2)
- Lines 941-1200: Add 3 new filter thread methods (Phase 3)
- Lines 560-578: Update `start()` method (Phase 4)
- Lines 943-1050: Add queue monitoring to `_display_loop` (Phase 6)

**Expected Diff:** +400 lines (3 new threads), -100 lines (simplified collection loops) = **+300 net lines**

---

## Rollback Plan

If refactor causes issues:

1. **Git revert** to commit `f7ead99` (Gyro fix, before refactor)
2. **Disable ES-EKF** in collection loops (already have this pattern)
3. **Keep new architecture** but fall back to synchronous filter calls in collection loops

---

## Notes

- ES-EKF hanging at sample #6 is root cause of this refactor
- Current workaround: ES-EKF disabled in `_accel_loop` (line 760 commented)
- This refactor allows ES-EKF to be re-enabled safely (failures isolated)
- Future: Investigate ES-EKF implementation to fix hang (separate task)

**Key Insight:** Data collection is critical path - filter processing is NOT.
