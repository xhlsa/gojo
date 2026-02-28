//! JNI bridge — Kotlin ↔ Rust EKF interface.
//!
//! Kotlin loads the library with `System.loadLibrary("gojo_core")` and calls
//! these functions via `external fun` declarations in `GojoJni.kt`.
//!
//! Design: the `SensorFusion` struct (and its origin tracking) lives entirely
//! in Rust-allocated memory.  Kotlin holds a single `Long` (opaque raw pointer)
//! as a handle.  No filter state is ever copied into Java heap.
//!
//! Thread safety: callers must not share a handle across threads without their
//! own synchronisation.  The recommended pattern (Step 3) is a single
//! `HandlerThread` that owns both IMU and GPS callbacks.

#![allow(non_snake_case)] // JNI naming convention requires Java_com_…_Method

use jni::objects::JClass;
use jni::sys::{jdoubleArray, jint, jlong};
use jni::JNIEnv;

use crate::sensor_fusion::{FusionConfig, FusionEvent, SensorFusion};
use crate::types::{AccelData, GpsData, GyroData};

// ─── Session state ────────────────────────────────────────────────────────────

/// Everything owned by one EKF session.
/// Kotlin holds a `jlong` (raw *mut GojoState) as an opaque handle.
struct GojoState {
    fusion: SensorFusion,
    /// WGS84 origin of the local ENU frame, captured on the first GPS fix.
    /// None until `ColdStartInitialized` fires.
    origin: Option<(f64, f64)>,
}

// ─── Coordinate helpers ───────────────────────────────────────────────────────

/// Convert ENU metres (East, North) back to WGS84 degrees around an origin.
fn enu_to_wgs84(east_m: f64, north_m: f64, origin_lat: f64, origin_lon: f64) -> (f64, f64) {
    const R: f64 = 6_371_000.0;
    let lat = origin_lat + (north_m / R).to_degrees();
    let lon = origin_lon + (east_m / (R * origin_lat.to_radians().cos())).to_degrees();
    (lat, lon)
}

/// Navigation bearing in [0, 360) degrees — 0 = North, clockwise.
/// Inputs are ENU velocity components.
fn bearing_deg(vx_east: f64, vy_north: f64) -> f64 {
    // atan2(east, north) gives angle from North toward East — standard bearing.
    f64::atan2(vx_east, vy_north).to_degrees().rem_euclid(360.0)
}

// ─── Event flags (returned to Kotlin as a jint bitmask) ──────────────────────

const FLAG_COLD_START: i32      = 1 << 0; // first GPS fix received
const FLAG_HEADING_ALIGNED: i32 = 1 << 1; // heading locked to GPS bearing
const FLAG_INCIDENT: i32        = 1 << 2; // braking / impact / swerve detected
const FLAG_GAP_ACTIVE: i32      = 1 << 3; // GPS gap mode entered / clamping
const FLAG_GAP_EXITED: i32      = 1 << 4; // GPS gap mode exited
const FLAG_GPS_REJECTED: i32    = 1 << 5; // GPS fix rejected (accuracy / jump)
const FLAG_ZUPT: i32            = 1 << 6; // zero-velocity update applied

fn events_to_flags(events: &[FusionEvent]) -> jint {
    let mut flags = 0i32;
    for event in events {
        match event {
            FusionEvent::ColdStartInitialized { .. } => flags |= FLAG_COLD_START,
            FusionEvent::HeadingAligned { .. }        => flags |= FLAG_HEADING_ALIGNED,
            FusionEvent::IncidentDetected(_)          => flags |= FLAG_INCIDENT,
            FusionEvent::GapClampActive { .. }        => flags |= FLAG_GAP_ACTIVE,
            FusionEvent::GapModeExited                => flags |= FLAG_GAP_EXITED,
            FusionEvent::GpsRejected { .. }           => flags |= FLAG_GPS_REJECTED,
            FusionEvent::ZuptApplied                  => flags |= FLAG_ZUPT,
            _                                         => {}
        }
    }
    flags
}

// ─── State extraction (no JNI in here — easier to unit-test) ─────────────────

/// Build the 12-element f64 array that `getState` returns to Kotlin.
///
/// Layout (indices match GojoJni.kt documentation):
///   [0]  latitude          (degrees WGS84)
///   [1]  longitude         (degrees WGS84)
///   [2]  altitude          (metres, EKF up-channel from origin — not absolute)
///   [3]  speed             (m/s, horizontal magnitude)
///   [4]  heading           (degrees, 0 = North, clockwise)
///   [5]  vel_north         (m/s)
///   [6]  vel_east          (m/s)
///   [7]  cov_trace         (position uncertainty proxy from EKF covariance)
///   [8]  roughness         (road surface estimate, 0–1+)
///   [9]  gps_gap_secs      (seconds since last accepted GPS fix)
///   [10] is_stationary     (1.0 = stationary, 0.0 = moving)
///   [11] heading_init      (1.0 = heading locked, 0.0 = not yet)
fn state_array(handle: jlong) -> [f64; 12] {
    if handle == 0 {
        return [0.0; 12];
    }

    let state = unsafe { &*(handle as *const GojoState) };
    let snap = state.fusion.get_snapshot();
    let ekf = &snap.ekf_15d_state;

    let (lat, lon) = match state.origin {
        Some((origin_lat, origin_lon)) => {
            enu_to_wgs84(ekf.position.0, ekf.position.1, origin_lat, origin_lon)
        }
        None => (0.0, 0.0), // GPS not yet acquired
    };

    let vx_east  = ekf.velocity.0;
    let vy_north = ekf.velocity.1;
    let speed    = (vx_east * vx_east + vy_north * vy_north).sqrt();
    let heading  = bearing_deg(vx_east, vy_north);

    [
        lat,
        lon,
        ekf.position.2,                              // EKF altitude (up-channel)
        speed,
        heading,
        vy_north,                                    // vel_north
        vx_east,                                     // vel_east
        ekf.covariance_trace,
        snap.roughness,
        snap.gps_gap_secs,
        if snap.is_stationary { 1.0 } else { 0.0 },
        if snap.heading_initialized { 1.0 } else { 0.0 },
    ]
}

// ─── JNI functions ────────────────────────────────────────────────────────────

/// Allocate a new EKF session.
///
/// Returns an opaque handle (raw pointer cast to jlong).  The caller MUST call
/// `destroy()` when done — this is the only way to free the Rust memory.
///
/// `dt` is set to 0.01 s (100 Hz) to match Android IMU callback rate.
#[no_mangle]
pub unsafe extern "C" fn Java_com_gojo_core_GojoJni_init(
    _env: JNIEnv,
    _class: JClass,
) -> jlong {
    let config = FusionConfig {
        dt: 0.01, // 100 Hz — Android SensorManager rate
        ..FusionConfig::default()
    };
    let state = Box::new(GojoState {
        fusion: SensorFusion::new(config),
        origin: None,
    });
    Box::into_raw(state) as jlong
}

/// Free an EKF session.  Call from Kotlin's `onDestroy`.
#[no_mangle]
pub unsafe extern "C" fn Java_com_gojo_core_GojoJni_destroy(
    _env: JNIEnv,
    _class: JClass,
    handle: jlong,
) {
    if handle != 0 {
        drop(Box::from_raw(handle as *mut GojoState));
    }
}

/// Override gravity and gyro bias from a stationary calibration preamble.
///
/// - `gx/gy/gz`: mean accelerometer reading while stationary (gravity vector, m/s²).
///   Typically ~(0, 0, 9.81) on a flat surface.
/// - `bx/by/bz`: mean gyroscope reading while stationary (gyro bias, rad/s).
///   Typically ~(0, 0, 0).
///
/// Call this after collecting 50+ stationary samples before the first drive.
#[no_mangle]
pub unsafe extern "C" fn Java_com_gojo_core_GojoJni_setCalibration(
    _env: JNIEnv,
    _class: JClass,
    handle: jlong,
    gx: f64,
    gy: f64,
    gz: f64,
    bx: f64,
    by: f64,
    bz: f64,
) {
    if handle == 0 { return; }
    let state = &mut *(handle as *mut GojoState);
    state.fusion.set_biases((gx, gy, gz), (bx, by, bz));
}

/// Feed one paired IMU sample (accel + gyro at the same timestamp).
///
/// - `ax/ay/az`: accelerometer reading (m/s², body frame).
/// - `gx/gy/gz`: gyroscope reading (rad/s, body frame).
/// - `timestamp_ns`: `SensorEvent.timestamp` — `elapsedRealtimeNanos`.
///
/// Returns a bitmask of `FLAG_*` constants indicating notable events.
/// Call at ~100 Hz from the sensor callback thread.
#[no_mangle]
pub unsafe extern "C" fn Java_com_gojo_core_GojoJni_processImu(
    _env: JNIEnv,
    _class: JClass,
    handle: jlong,
    ax: f64,
    ay: f64,
    az: f64,
    gx: f64,
    gy: f64,
    gz: f64,
    timestamp_ns: jlong,
) -> jint {
    if handle == 0 { return 0; }
    let state = &mut *(handle as *mut GojoState);

    // Android timestamps are elapsedRealtimeNanos — convert to seconds for the filter.
    let ts = timestamp_ns as f64 * 1e-9;

    let accel = AccelData { timestamp: ts, x: ax, y: ay, z: az };
    let gyro  = GyroData  { timestamp: ts, x: gx, y: gy, z: gz };

    let mut events = state.fusion.feed_accel(&accel);
    events.extend(state.fusion.feed_gyro(&gyro));
    // tick() handles ZUPT, dynamic gravity refinement, and es_ekf predict.
    events.extend(state.fusion.tick());

    events_to_flags(&events)
}

/// Feed one GPS fix.
///
/// - `lat/lon`: WGS84 degrees.
/// - `alt`: metres (passed through but EKF does not fuse altitude).
/// - `accuracy`: horizontal accuracy (metres), from `Location.getAccuracy()`.
/// - `speed`: ground speed (m/s), from `Location.getSpeed()`.
/// - `bearing`: heading (degrees), from `Location.getBearing()`.
/// - `timestamp_ns`: `Location.getElapsedRealtimeNanos()` — same clock as IMU.
///
/// Returns `FLAG_COLD_START` on the first accepted fix (filter origin set).
/// Call at ~1 Hz from the location callback.
#[no_mangle]
pub unsafe extern "C" fn Java_com_gojo_core_GojoJni_processGps(
    _env: JNIEnv,
    _class: JClass,
    handle: jlong,
    lat: f64,
    lon: f64,
    _alt: f64,
    accuracy: f32,
    speed: f32,
    bearing: f32,
    timestamp_ns: jlong,
) -> jint {
    if handle == 0 { return 0; }
    let state = &mut *(handle as *mut GojoState);

    let ts = timestamp_ns as f64 * 1e-9;
    let gps = GpsData {
        timestamp: ts,
        latitude:  lat,
        longitude: lon,
        speed:     speed as f64,
        bearing:   bearing as f64,
        accuracy:  accuracy as f64,
    };

    // Pass ts as system_time — no external latency correction on Android
    // because elapsedRealtimeNanos is the same clock on both sensor paths.
    let events = state.fusion.feed_gps(&gps, ts);

    // Capture the ENU origin from the cold-start event (first GPS fix).
    for event in &events {
        if let FusionEvent::ColdStartInitialized { lat: origin_lat, lon: origin_lon } = event {
            if state.origin.is_none() {
                state.origin = Some((*origin_lat, *origin_lon));
            }
        }
    }

    events_to_flags(&events)
}

/// Return current filter state as a 12-element `DoubleArray`.
///
/// See `state_array()` docs for the element layout.
/// Returns an all-zero array if no GPS fix has been received yet.
#[no_mangle]
pub unsafe extern "C" fn Java_com_gojo_core_GojoJni_getState(
    mut env: JNIEnv,
    _class: JClass,
    handle: jlong,
) -> jdoubleArray {
    let data = state_array(handle);

    match env.new_double_array(12) {
        Ok(arr) => {
            let _ = env.set_double_array_region(&arr, 0, &data);
            arr.into_raw()
        }
        // OOM on the Java side — return null (Kotlin caller checks for NPE).
        Err(_) => std::ptr::null_mut(),
    }
}
