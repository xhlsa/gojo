# Phase 3: End-to-End Sensor Pipeline Integration

**Status:** ✅ **COMPLETE** (Phases 3A-3E)
**Last Updated:** Nov 19, 2025
**Commits:** 864cc59, 671363c, 717d17a
**Next:** Phase 4 (Optional: Bound Service + Dashboard UI)

## Overview

Phase 3 implements the complete end-to-end sensor data pipeline for motion tracking on Android:
- **Data Classes & ViewModel** (3A) - State management layer
- **Rust JNI Extensions** (3B) - Config-aware session initialization
- **Service Lifecycle & Real-Time Updates** (3C) - Notification ticker + LiveData
- **Session Persistence** (3D) - 15-second chunk streaming to app storage
- **Permission Flow** (3E) - Runtime permissions gated on Start button

## Quick Start

### Build
```bash
cd motion-tracker-android
./gradlew assembleDebug
./gradlew installDebug
```

### Run
1. Launch app → Grant permissions (LOCATION + BODY_SENSORS)
2. Tap **Start** → Session begins, notification shows "Recording • elapsed time"
3. Wait 30+ seconds → Chunks written to `/sdcard/Android/data/<pkg>/files/sessions/`
4. Tap **Stop** → final.json exported, session saved

### Verify
```bash
# Check session files
adb shell ls /sdcard/Android/data/com.example.motiontracker/files/sessions/session_*/

# Pull final export
adb pull /sdcard/Android/data/com.example.motiontracker/files/sessions/session_*/final.json
jq '.' final.json | head -20
```

## Architecture

```
┌─────────────────────────────────────────┐
│ Activity (Permissions + Session Control) │
│  ├─ SessionViewModel (State Management)  │
│  └─ Permission flow (Start button)       │
└──────────────────┬──────────────────────┘
                   │
         ┌─────────┴─────────┐
         ▼                   ▼
    ┌─────────────────┐  ┌──────────────┐
    │ Service         │  │ Rust JNI Core│
    │ ├─ Sensors      │  │ ├─ SessionCfg│
    │ ├─ GPS          │  │ ├─ Queues    │
    │ ├─ Health Mon   │  │ └─ State Mgmt│
    │ ├─ Notifier     │  └──────────────┘
    │ └─ SessionWriter│
    └─────────────────┘

Storage: /sdcard/Android/data/<pkg>/files/sessions/
  ├─ session_20241119_143022/
  │  ├─ metadata.json
  │  ├─ chunks/chunk_N.json (15s intervals)
  │  └─ final.json (complete export)
```

## Phase Breakdown

| Phase | Component | Lines | Status |
|-------|-----------|-------|--------|
| **3A** | SessionViewModel + Data Classes | 500+ | ✅ Complete |
| **3B** | Rust SessionConfig + JNI bridge | 150+ | ✅ Complete |
| **3C** | Service lifecycle + Notification ticker | 300+ | ✅ Complete |
| **3D** | SessionWriter + SessionStorage | 550+ | ✅ Complete |
| **3E** | Permission flow + ViewModel integration | 400+ | ✅ Complete |
| **Total** | End-to-end pipeline | **1,900+** | ✅ Complete |

## Files Created/Modified

### New Files
```
app/src/main/kotlin/com/example/motiontracker/
├─ SessionViewModel.kt                      (230 lines)
├─ SessionWriter.kt                         (280 lines)
└─ data/
   ├─ GpsStatus.kt                          (35 lines)
   ├─ HealthAlert.kt                        (40 lines)
   └─ SessionConfig.kt                      (40 lines)
app/src/main/kotlin/com/example/motiontracker/storage/
└─ SessionStorage.kt                        (230 lines)
rust/src/
└─ (SessionConfig struct added to session.rs)
```

### Modified Files
```
app/
├─ build.gradle.kts                         (+dependencies)
├─ src/main/AndroidManifest.xml             (+permissions)
└─ src/main/kotlin/com/example/motiontracker/
   ├─ MotionTrackerActivity.kt              (refactored for permissions)
   ├─ MotionTrackerService.kt               (+ticker +writer integration)
   ├─ JniBinding.kt                         (+startSessionWithConfig)
   └─ LocationCollector.kt                  (+GPS status publishing)
rust/src/
├─ session.rs                               (+SessionConfig +with_config factory)
├─ android_jni.rs                           (+startSessionWithConfig JNI)
└─ lib.rs                                   (+SessionConfig export)
```

## Key Features

### 1. Permissions (Phase 3E)
- Gated on Start button (no early permission check)
- Persistent dialog if denied (no fallback)
- Covers Android 6-14+ with background location for Android 10+

```kotlin
// Check: LOCATION (fine + coarse) + BODY_SENSORS + BACKGROUND_LOCATION (10+)
if (!hasRequiredPermissions()) {
    requestRequiredPermissions()
    return
}
```

### 2. Session Lifecycle (Phases 3C + 3E)
```
Start Button
  ├─ Check Permissions
  ├─ SessionConfig.default() (device model + rates)
  ├─ JniBinding.startSessionWithConfig(config)
  ├─ Service.startSessionWriter(config)
  └─ ViewModel.startRecording()

Stop Button
  ├─ Service.finalizeSessionWriter() → final.json
  ├─ JniBinding.stopSession()
  └─ ViewModel.stopRecording()
```

### 3. Session Storage (Phase 3D)
Periodic chunk writing every ~15 seconds:
```
SessionWriter.writerLoop()
  → JniBinding.getSessionJson() (export current session)
    → SessionStorage.writeChunk(index, json)
      → /sdcard/Android/data/<pkg>/files/sessions/session_XXX/chunks/chunk_N.json
```

Final export on stop:
```
Service.finalizeSessionWriter()
  → SessionWriter.finalize()
    → final.json (all samples + metadata + stats)
```

### 4. Real-Time Updates (Phase 3C)
Handler-based notification ticker (1s interval):
```
updateNotificationTick()
  ├─ elapsedSeconds++
  ├─ JniBinding.getSampleCountsLabeled()
  ├─ Build: "Recording • 1m 23s • A:245 G:240 P:47"
  └─ NotificationManager.notify()
```

### 5. Service Integration (Phase 3B)
Config-aware JNI session:
```rust
// Rust: Session with device parameters
pub fn startSessionWithConfig(config_json: String)
  → SessionConfig::deserialize(config_json)
  → Session::with_config(config)
  → Recording begins with device-specific EKF tuning
```

## Testing

See [PHASE3_SENSOR_INTEGRATION.md](./PHASE3_SENSOR_INTEGRATION.md) for comprehensive test checklist covering:
- Permission flow
- Sensor data collection
- Chunk writing
- GPS accuracy
- Memory bounds
- Error scenarios
- Lifecycle robustness

Quick test:
```bash
./gradlew installDebug
# 1. Grant permissions
# 2. Tap Start
# 3. Wait 30s (2 chunks)
# 4. Tap Stop
# 5. Pull final.json
```

## Performance

| Metric | Expected | Notes |
|--------|----------|-------|
| Startup | <5s | Service + sensor init |
| Memory | 90-110 MB | Stable (bounded queues) |
| Chunk Write | <100ms | Every 15s |
| CPU | 15-25% | 50 Hz sensors |
| Battery | ~8-10%/hour | Accel + gyro + GPS |

## Error Handling

- **Permission Denied**: Show persistent dialog + disable Start
- **Sensor Failure**: HealthMonitor auto-restart + graceful degrade
- **GPS Timeout**: Continue without GPS (accel+gyro only)
- **Disk Full**: Warning logged, continue writing (chunks may fail)
- **JNI Export Failed**: Retry next 15s cycle

All failures graceful (no hard crashes unless fatal permission denial).

## Debugging

```bash
# Watch all logs
adb logcat | grep MotionTracker

# Sensor activity
adb logcat | grep "MotionTracker.Sensors"

# Service lifecycle
adb logcat | grep "MotionTracker.Service"

# Permission flow
adb logcat | grep "Permission\|required"

# Session files
adb shell ls /sdcard/Android/data/com.example.motiontracker/files/sessions/session_*/

# Pull + inspect
adb pull /sdcard/Android/data/com.example.motiontracker/files/sessions/session_*/final.json
jq '.metadata' final.json  # Device info, sample counts
jq '.accel_samples | length' final.json  # Total accel samples
```

## Next Steps (Phase 4+)

- Bound Service (AIDL) for robust Activity-Service communication
- In-app dashboard with map visualization
- Dynamic sampling rate
- SQLite offline queue
- Real-time filter metrics display

## Commits

- **864cc59** - Phase 3A-3C: Data layer, Rust config, service lifecycle
- **671363c** - Phase 3D: SessionWriter + SessionStorage persistence
- **717d17a** - Phase 3E: Permission flow + Activity integration

---

For complete details, see:
- [PHASE3_SENSOR_INTEGRATION.md](./PHASE3_SENSOR_INTEGRATION.md) - Test checklist
- Phase 3A-3E commit messages for implementation details

**Built with Claude Code** 🤖
Verified: Nov 19, 2025 | Rust: cargo check ✓
