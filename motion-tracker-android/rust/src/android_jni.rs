use crate::error::{throw_java_exception, MotionTrackerError, JResult};
use crate::sensor_receiver::{AccelSample, GpsSample, GyroSample};
use crate::session::{Session, SessionState};
use jni::objects::JClass;
use jni::sys::{jdouble, jint, jstring, jintArray};
use jni::JNIEnv;
use std::sync::{Arc, Mutex};
use crate::log_info;

// Global session state - stored as static to persist across JNI calls
lazy_static::lazy_static! {
    static ref GLOBAL_SESSION: Arc<Mutex<Option<Arc<Session>>>> = Arc::new(Mutex::new(None));
}

/// Get or create the current session
fn get_session() -> JResult<Arc<Session>> {
    let mut session_guard = GLOBAL_SESSION.lock().map_err(|_| {
        MotionTrackerError::Internal("Failed to acquire global session lock".to_string())
    })?;

    match session_guard.as_ref() {
        Some(session) => Ok(Arc::clone(session)),
        None => {
            let session = Arc::new(Session::new());
            *session_guard = Some(Arc::clone(&session));
            Ok(session)
        }
    }
}

/// JNI: Start a new recording session
/// Returns: 0 on success, -1 on error (throws Java exception)
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_startSession(
    mut env: JNIEnv,
    _class: JClass,
) -> jint {
    match start_session_impl(&mut env) {
        Ok(_) => 0,
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            -1
        }
    }
}

fn start_session_impl(env: &mut JNIEnv) -> JResult<()> {
    let session = get_session()?;
    session.start_recording()?;

    // Log to Android
    android_log(env, "MotionTracker", "Session started");

    Ok(())
}

/// JNI: Stop current recording session
/// Returns: 0 on success, -1 on error (throws Java exception)
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_stopSession(
    mut env: JNIEnv,
    _class: JClass,
) -> jint {
    match stop_session_impl(&mut env) {
        Ok(_) => 0,
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            -1
        }
    }
}

fn stop_session_impl(env: &mut JNIEnv) -> JResult<()> {
    let session = get_session()?;
    session.stop_recording()?;

    // Log to Android
    android_log(env, "MotionTracker", "Session stopped");

    Ok(())
}

/// JNI: Pause current recording session
/// Returns: 0 on success, -1 on error (throws Java exception)
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_pauseSession(
    mut env: JNIEnv,
    _class: JClass,
) -> jint {
    match pause_session_impl(&mut env) {
        Ok(_) => 0,
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            -1
        }
    }
}

fn pause_session_impl(env: &mut JNIEnv) -> JResult<()> {
    let session = get_session()?;
    session.pause_recording()?;

    android_log(env, "MotionTracker", "Session paused");

    Ok(())
}

/// JNI: Resume recording (Paused → Recording)
/// Returns: 0 on success, -1 on error (throws Java exception)
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_resumeSession(
    mut env: JNIEnv,
    _class: JClass,
) -> jint {
    match resume_session_impl(&mut env) {
        Ok(_) => 0,
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            -1
        }
    }
}

fn resume_session_impl(env: &mut JNIEnv) -> JResult<()> {
    let session = get_session()?;
    session.start_recording()?;

    android_log(env, "MotionTracker", "Session resumed");

    Ok(())
}

/// JNI: Push accelerometer sample
/// Parameters: x, y, z (m/s²), timestamp (seconds since epoch)
/// Returns: 0 on success, -1 on error (throws Java exception)
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_pushAccelSample(
    mut env: JNIEnv,
    _class: JClass,
    x: jdouble,
    y: jdouble,
    z: jdouble,
    timestamp: jdouble,
) -> jint {
    match push_accel_sample_impl(x as f64, y as f64, z as f64, timestamp as f64) {
        Ok(_) => 0,
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            -1
        }
    }
}

fn push_accel_sample_impl(x: f64, y: f64, z: f64, timestamp: f64) -> JResult<()> {
    let session = get_session()?;
    let sample = AccelSample::new(x, y, z, timestamp);
    session.push_accel_sample(sample)?;
    Ok(())
}

/// JNI: Push gyroscope sample
/// Parameters: x, y, z (rad/s), timestamp (seconds since epoch)
/// Returns: 0 on success, -1 on error (throws Java exception)
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_pushGyroSample(
    mut env: JNIEnv,
    _class: JClass,
    x: jdouble,
    y: jdouble,
    z: jdouble,
    timestamp: jdouble,
) -> jint {
    match push_gyro_sample_impl(x as f64, y as f64, z as f64, timestamp as f64) {
        Ok(_) => 0,
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            -1
        }
    }
}

fn push_gyro_sample_impl(x: f64, y: f64, z: f64, timestamp: f64) -> JResult<()> {
    let session = get_session()?;
    let sample = GyroSample::new(x, y, z, timestamp);
    session.push_gyro_sample(sample)?;
    Ok(())
}

/// JNI: Push GPS sample
/// Parameters: latitude, longitude, altitude (m), accuracy (m), speed (m/s), bearing (degrees), timestamp
/// Returns: 0 on success, -1 on error (throws Java exception)
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_pushGpsSample(
    mut env: JNIEnv,
    _class: JClass,
    latitude: jdouble,
    longitude: jdouble,
    altitude: jdouble,
    accuracy: jdouble,
    speed: jdouble,
    bearing: jdouble,
    timestamp: jdouble,
) -> jint {
    match push_gps_sample_impl(
        latitude as f64,
        longitude as f64,
        altitude as f64,
        accuracy as f64,
        speed as f64,
        bearing as f64,
        timestamp as f64,
    ) {
        Ok(_) => 0,
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            -1
        }
    }
}

fn push_gps_sample_impl(
    latitude: f64,
    longitude: f64,
    altitude: f64,
    accuracy: f64,
    speed: f64,
    bearing: f64,
    timestamp: f64,
) -> JResult<()> {
    let session = get_session()?;
    let sample = GpsSample::new(latitude, longitude, altitude, accuracy, speed, bearing, timestamp);
    session.push_gps_sample(sample)?;
    Ok(())
}

/// JNI: Get current session state as string
/// Returns: "IDLE", "RECORDING", or "PAUSED" as jstring
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_getSessionState(
    mut env: JNIEnv,
    _class: JClass,
) -> jstring {
    match get_session_state_impl() {
        Ok(state_str) => match env.new_string(&state_str) {
            Ok(jstr) => jstr.into_raw(),
            Err(_) => {
                let _ = throw_java_exception(&mut env, &MotionTrackerError::JniError(
                    "Failed to create Java string".to_string(),
                ));
                std::ptr::null_mut()
            }
        },
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            std::ptr::null_mut()
        }
    }
}

fn get_session_state_impl() -> JResult<String> {
    let session = get_session()?;
    let state = session.get_state()?;
    let state_str = match state {
        SessionState::Idle => "IDLE",
        SessionState::Recording => "RECORDING",
        SessionState::Paused => "PAUSED",
    };
    Ok(state_str.to_string())
}

/// JNI: Get sample counts [accel, gyro, gps]
/// Returns: jintArray with 3 elements
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_getSampleCounts(
    mut env: JNIEnv,
    _class: JClass,
) -> jintArray {
    match get_sample_counts_impl() {
        Ok(counts) => match env.new_int_array(3) {
            Ok(arr) => {
                let _ = env.set_int_array_region(&arr, 0, &counts);
                arr.into_raw()
            }
            Err(_) => std::ptr::null_mut(),
        },
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            std::ptr::null_mut()
        }
    }
}

fn get_sample_counts_impl() -> JResult<[i32; 3]> {
    let session = get_session()?;
    let meta = session.get_metadata()?;
    Ok([
        meta.accel_sample_count as i32,
        meta.gyro_sample_count as i32,
        meta.gps_sample_count as i32,
    ])
}

/// JNI: Export session as JSON string
/// Returns: JSON string or null on error (throws Java exception)
#[no_mangle]
pub extern "C" fn Java_com_example_motiontracker_JniBinding_getSessionJson(
    mut env: JNIEnv,
    _class: JClass,
) -> jstring {
    match get_session_json_impl() {
        Ok(json_str) => match env.new_string(&json_str) {
            Ok(jstr) => jstr.into_raw(),
            Err(_) => {
                let _ = throw_java_exception(&mut env, &MotionTrackerError::JniError(
                    "Failed to create Java string".to_string(),
                ));
                std::ptr::null_mut()
            }
        },
        Err(e) => {
            let _ = throw_java_exception(&mut env, &e);
            std::ptr::null_mut()
        }
    }
}

fn get_session_json_impl() -> JResult<String> {
    let session = get_session()?;
    let export = session.export()?;
    export.to_json()
        .map_err(|_| MotionTrackerError::Internal("JSON serialization failed".to_string()))
}

/// Helper: Log to Android Logcat
#[allow(dead_code)]
fn android_log(_env: &mut JNIEnv, tag: &str, msg: &str) {
    // Log to Android system log via stderr (captured by logcat)
    // Format: [TAG] message for proper Android log filtering
    log_info!("[{}] {}", tag, msg);
}
