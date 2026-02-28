pub mod filters;
pub mod incident;
pub mod sensor_fusion;
pub mod smoothing;
pub mod types;

// JNI bridge — compiled only when targeting Android.
// The desktop binaries (motion_tracker, replay) never include this.
#[cfg(target_os = "android")]
pub mod jni;
