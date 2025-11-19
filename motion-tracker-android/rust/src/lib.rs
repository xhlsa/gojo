// Motion Tracker Android JNI Library
// Exposes Rust motion tracking core to Kotlin via JNI

pub mod android_jni;
pub mod error;
pub mod sensor_receiver;
pub mod session;
pub mod storage;

// Re-export public types for potential future usage
pub use error::{MotionTrackerError, JResult};
pub use sensor_receiver::{AccelSample, GpsSample, GyroSample, SensorReading};
pub use session::{Session, SessionMetadata, SessionState};
pub use storage::{SessionExport, GpxTrack, SessionStats};

// Android logging infrastructure
pub mod logging {
    /// Log to Android system log via stderr (captured by Android logs)
    /// Example: log_info!("MotionTracker: Starting session")
    #[macro_export]
    macro_rules! log_info {
        ($($arg:tt)*) => {
            eprintln!("[INFO] {}", format!($($arg)*));
        };
    }

    #[macro_export]
    macro_rules! log_warn {
        ($($arg:tt)*) => {
            eprintln!("[WARN] {}", format!($($arg)*));
        };
    }

    #[macro_export]
    macro_rules! log_error {
        ($($arg:tt)*) => {
            eprintln!("[ERROR] {}", format!($($arg)*));
        };
    }

    #[macro_export]
    macro_rules! log_debug {
        ($($arg:tt)*) => {
            #[cfg(debug_assertions)]
            eprintln!("[DEBUG] {}", format!($($arg)*));
        };
    }
}
