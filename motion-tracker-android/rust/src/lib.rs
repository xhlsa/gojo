// Motion Tracker Android JNI Library
// Exposes Rust motion tracking core to Kotlin via JNI

pub mod android_jni;
pub mod error;
pub mod sensor_receiver;
pub mod session;

// Re-export public types for potential future usage
pub use error::{MotionTrackerError, JResult};
pub use sensor_receiver::{AccelSample, GpsSample, GyroSample, SensorReading};
pub use session::{Session, SessionMetadata, SessionState};
