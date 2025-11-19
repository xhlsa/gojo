use crate::error::{JResult, MotionTrackerError};
use crate::sensor_receiver::{AccelSample, GpsSample, GyroSample};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

/// Session state machine states
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SessionState {
    /// Service created but not recording
    Idle,
    /// Recording sensor data
    Recording,
    /// Paused (not recording but service alive)
    Paused,
}

/// Session metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionMetadata {
    pub session_id: String,
    pub start_time: String,
    pub state: SessionState,
    pub accel_sample_count: u32,
    pub gyro_sample_count: u32,
    pub gps_sample_count: u32,
    pub distance_meters: f64,
    pub peak_speed_ms: f64,
}

/// Motion tracking session
pub struct Session {
    metadata: Arc<Mutex<SessionMetadata>>,
    accel_queue: Arc<Mutex<VecDeque<AccelSample>>>,
    gyro_queue: Arc<Mutex<VecDeque<GyroSample>>>,
    gps_queue: Arc<Mutex<VecDeque<GpsSample>>>,
}

impl Session {
    /// Create new session in Idle state
    pub fn new() -> Self {
        let session_id = format!("session_{}", Utc::now().timestamp_millis());
        let start_time = Utc::now().to_rfc3339();

        let metadata = SessionMetadata {
            session_id,
            start_time,
            state: SessionState::Idle,
            accel_sample_count: 0,
            gyro_sample_count: 0,
            gps_sample_count: 0,
            distance_meters: 0.0,
            peak_speed_ms: 0.0,
        };

        Session {
            metadata: Arc::new(Mutex::new(metadata)),
            accel_queue: Arc::new(Mutex::new(VecDeque::with_capacity(500))),
            gyro_queue: Arc::new(Mutex::new(VecDeque::with_capacity(500))),
            gps_queue: Arc::new(Mutex::new(VecDeque::with_capacity(100))),
        }
    }

    /// Transition to Recording state (Idle → Recording)
    pub fn start_recording(&self) -> JResult<()> {
        let mut meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;

        match meta.state {
            SessionState::Idle => {
                meta.state = SessionState::Recording;
                Ok(())
            }
            SessionState::Recording => Err(MotionTrackerError::AlreadyRunning),
            SessionState::Paused => {
                meta.state = SessionState::Recording;
                Ok(())
            }
        }
    }

    /// Transition to Paused state (Recording → Paused)
    pub fn pause_recording(&self) -> JResult<()> {
        let mut meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;

        match meta.state {
            SessionState::Recording => {
                meta.state = SessionState::Paused;
                Ok(())
            }
            SessionState::Paused => Err(MotionTrackerError::InvalidState(
                "Already paused".to_string(),
            )),
            SessionState::Idle => {
                Err(MotionTrackerError::InvalidState("Not recording".to_string()))
            }
        }
    }

    /// Transition to Idle state (Paused → Idle, ends session)
    pub fn stop_recording(&self) -> JResult<()> {
        let mut meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;

        match meta.state {
            SessionState::Recording | SessionState::Paused => {
                meta.state = SessionState::Idle;
                Ok(())
            }
            SessionState::Idle => Err(MotionTrackerError::NotRunning),
        }
    }

    /// Get current state
    pub fn get_state(&self) -> JResult<SessionState> {
        let meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;
        Ok(meta.state)
    }

    /// Check if currently recording
    pub fn is_recording(&self) -> JResult<bool> {
        let meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;
        Ok(meta.state == SessionState::Recording)
    }

    /// Add accelerometer sample
    pub fn push_accel_sample(&self, sample: AccelSample) -> JResult<()> {
        // Only accept samples while recording
        if !self.is_recording()? {
            return Ok(());
        }

        let mut queue = self.accel_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire accel queue lock".to_string())
        })?;

        queue.push_back(sample);

        // Update metadata
        let mut meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;
        meta.accel_sample_count += 1;

        Ok(())
    }

    /// Add gyroscope sample
    pub fn push_gyro_sample(&self, sample: GyroSample) -> JResult<()> {
        if !self.is_recording()? {
            return Ok(());
        }

        let mut queue = self.gyro_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire gyro queue lock".to_string())
        })?;

        queue.push_back(sample);

        let mut meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;
        meta.gyro_sample_count += 1;

        Ok(())
    }

    /// Add GPS sample
    pub fn push_gps_sample(&self, sample: GpsSample) -> JResult<()> {
        if !self.is_recording()? {
            return Ok(());
        }

        // Store peak speed before moving sample to queue
        let peak_speed = sample.speed;

        let mut queue = self.gps_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire gps queue lock".to_string())
        })?;

        queue.push_back(sample);

        let mut meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;
        meta.gps_sample_count += 1;

        // Update peak speed
        if peak_speed > meta.peak_speed_ms {
            meta.peak_speed_ms = peak_speed;
        }

        Ok(())
    }

    /// Get metadata snapshot
    pub fn get_metadata(&self) -> JResult<SessionMetadata> {
        let meta = self.metadata.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire metadata lock".to_string())
        })?;
        Ok(meta.clone())
    }

    /// Get current queue sizes
    pub fn get_queue_sizes(&self) -> JResult<(usize, usize, usize)> {
        let accel_size = self.accel_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire accel queue lock".to_string())
        })?.len();

        let gyro_size = self.gyro_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire gyro queue lock".to_string())
        })?.len();

        let gps_size = self.gps_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire gps queue lock".to_string())
        })?.len();

        Ok((accel_size, gyro_size, gps_size))
    }

    /// Clear all queues (called on stop or auto-save)
    pub fn clear_queues(&self) -> JResult<()> {
        self.accel_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire accel queue lock".to_string())
        })?.clear();

        self.gyro_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire gyro queue lock".to_string())
        })?.clear();

        self.gps_queue.lock().map_err(|_| {
            MotionTrackerError::Internal("Failed to acquire gps queue lock".to_string())
        })?.clear();

        Ok(())
    }
}

impl Default for Session {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_session_state_transitions() {
        let session = Session::new();

        // Initial state is Idle
        assert_eq!(session.get_state().unwrap(), SessionState::Idle);
        assert!(!session.is_recording().unwrap());

        // Idle → Recording
        session.start_recording().unwrap();
        assert_eq!(session.get_state().unwrap(), SessionState::Recording);
        assert!(session.is_recording().unwrap());

        // Recording → Paused
        session.pause_recording().unwrap();
        assert_eq!(session.get_state().unwrap(), SessionState::Paused);
        assert!(!session.is_recording().unwrap());

        // Paused → Recording
        session.start_recording().unwrap();
        assert_eq!(session.get_state().unwrap(), SessionState::Recording);

        // Recording → Idle (stop)
        session.stop_recording().unwrap();
        assert_eq!(session.get_state().unwrap(), SessionState::Idle);
    }

    #[test]
    fn test_invalid_state_transitions() {
        let session = Session::new();

        // Can't pause while idle
        assert!(session.pause_recording().is_err());

        // Can't start twice
        session.start_recording().unwrap();
        assert!(session.start_recording().is_err());

        // Can pause once
        assert!(session.pause_recording().is_ok());

        // Can't pause twice
        assert!(session.pause_recording().is_err());
    }

    #[test]
    fn test_sample_counting() {
        let session = Session::new();
        session.start_recording().unwrap();

        let accel = AccelSample::new(1.0, 2.0, 3.0, 0.0);
        session.push_accel_sample(accel).unwrap();

        let meta = session.get_metadata().unwrap();
        assert_eq!(meta.accel_sample_count, 1);
    }
}
