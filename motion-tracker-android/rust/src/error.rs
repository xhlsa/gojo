use jni::JNIEnv;
use thiserror::Error;

/// Motion tracker error types
#[derive(Error, Debug, Clone)]
pub enum MotionTrackerError {
    #[error("Session already running")]
    AlreadyRunning,

    #[error("Session not running")]
    NotRunning,

    #[error("Invalid session state: {0}")]
    InvalidState(String),

    #[error("Sensor failed: {0}")]
    SensorFailed(String),

    #[error("Storage error: {0}")]
    StorageError(String),

    #[error("Invalid parameters: {0}")]
    InvalidParameters(String),

    #[error("JNI error: {0}")]
    JniError(String),

    #[error("Internal error: {0}")]
    Internal(String),
}

/// Result type for JNI operations
pub type JResult<T> = Result<T, MotionTrackerError>;

/// Throw Java exception from Rust error
pub fn throw_java_exception(env: &mut JNIEnv, error: &MotionTrackerError) -> JResult<()> {
    let exception_class = match error {
        MotionTrackerError::AlreadyRunning | MotionTrackerError::NotRunning => {
            "java/lang/IllegalStateException"
        }
        MotionTrackerError::InvalidState(_) | MotionTrackerError::InvalidParameters(_) => {
            "java/lang/IllegalArgumentException"
        }
        MotionTrackerError::SensorFailed(_) | MotionTrackerError::StorageError(_) => {
            "java/io/IOException"
        }
        MotionTrackerError::JniError(_) | MotionTrackerError::Internal(_) => {
            "java/lang/RuntimeException"
        }
    };

    let message = error.to_string();
    env.throw_new(exception_class, message)
        .map_err(|_| MotionTrackerError::JniError("Failed to throw exception".to_string()))?;

    Ok(())
}
