use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const CIRCUIT_BREAKER_WINDOW: Duration = Duration::from_secs(10);
const CIRCUIT_BREAKER_FAILS: usize = 5;

/// Tracks restart state for a single sensor
#[derive(Clone, Debug)]
pub struct RestartState {
    pub name: String,
    pub restart_needed: bool,
    pub next_retry_time: Instant,
    pub attempts: u32,
    pub max_attempts: u32,
    pub base_cooldown: Duration,
    pub current_cooldown: Duration,
    failure_window: VecDeque<Instant>,
    circuit_tripped: bool,
}

impl RestartState {
    pub fn new(name: &str, max_attempts: u32, base_cooldown_secs: u64) -> Self {
        let base_cooldown = Duration::from_secs(base_cooldown_secs);
        RestartState {
            name: name.to_string(),
            restart_needed: false,
            next_retry_time: Instant::now(),
            attempts: 0,
            max_attempts,
            base_cooldown,
            current_cooldown: base_cooldown,
            failure_window: VecDeque::with_capacity(CIRCUIT_BREAKER_FAILS + 1),
            circuit_tripped: false,
        }
    }

    /// Signal that this sensor needs restart
    pub fn signal_restart(&mut self) {
        self.restart_needed = true;
    }

    /// Check if enough time has passed for retry
    pub fn can_retry(&self) -> bool {
        Instant::now() >= self.next_retry_time && self.restart_needed
    }

    /// Record a failed restart attempt and calculate next retry time
    pub fn record_failed_attempt(&mut self) {
        self.attempts += 1;

        self.record_failure_window();

        // Exponential backoff: multiply cooldown by 1.5 each time, cap at 30 seconds
        self.current_cooldown = Duration::from_secs_f64(
            (self.current_cooldown.as_secs_f64() * 1.5).min(30.0),
        );

        self.next_retry_time = Instant::now() + self.current_cooldown;

        eprintln!(
            "[RESTART] {} restart attempt {} failed, next retry in {:.1}s (capped at 30s)",
            self.name,
            self.attempts,
            self.current_cooldown.as_secs_f64()
        );
    }

    /// Record a successful restart and reset state
    pub fn record_success(&mut self) {
        eprintln!(
            "[RESTART] ✓ {} restarted successfully after {} attempt(s)",
            self.name, self.attempts
        );
        self.restart_needed = false;
        self.attempts = 0;
        self.current_cooldown = self.base_cooldown;
        self.next_retry_time = Instant::now();
        self.failure_window.clear();
        self.circuit_tripped = false;
    }

    /// Check if max attempts exceeded
    pub fn can_restart(&self) -> bool {
        self.attempts < self.max_attempts
    }

    pub fn circuit_tripped(&self) -> bool {
        self.circuit_tripped
    }

    /// Get formatted status
    pub fn status(&self) -> String {
        if !self.restart_needed {
            return format!("{}: OK", self.name);
        }

        if !self.can_retry() {
            let wait_time = (self.next_retry_time - Instant::now()).as_secs_f64();
            return format!(
                "{}: RESTART_PENDING (waiting {:.1}s, attempt {}/{})",
                self.name, wait_time, self.attempts, self.max_attempts
            );
        }

        if !self.can_restart() {
            return format!("{}: MAX_ATTEMPTS_EXCEEDED", self.name);
        }

        format!(
            "{}: READY_TO_RESTART (attempt {}/{})",
            self.name, self.attempts, self.max_attempts
        )
    }

    fn record_failure_window(&mut self) {
        let now = Instant::now();
        self.failure_window.push_back(now);

        while let Some(front) = self.failure_window.front() {
            if now.duration_since(*front) > CIRCUIT_BREAKER_WINDOW {
                self.failure_window.pop_front();
            } else {
                break;
            }
        }

        if self.failure_window.len() >= CIRCUIT_BREAKER_FAILS {
            self.circuit_tripped = true;
            self.restart_needed = false;
            eprintln!(
                "[RESTART] {} circuit breaker tripped ({} failures in {:.0?}); shutting down restarts",
                self.name,
                self.failure_window.len(),
                CIRCUIT_BREAKER_WINDOW
            );
        }
    }
}

/// Manages restart state for all sensors
pub struct RestartManager {
    pub accel: Arc<Mutex<RestartState>>,
    pub gyro: Arc<Mutex<RestartState>>,
    pub gps: Arc<Mutex<RestartState>>,
}

impl RestartManager {
    pub fn new() -> Self {
        // Configuration from CLAUDE.md:
        // - Max restart attempts: 60 per sensor
        // - Base cooldown: 2 seconds (will exponentially backoff)
        RestartManager {
            accel: Arc::new(Mutex::new(RestartState::new("Accel", 60, 2))),
            gyro: Arc::new(Mutex::new(RestartState::new("Gyro", 60, 2))),
            gps: Arc::new(Mutex::new(RestartState::new("GPS", 60, 2))),
        }
    }

    /// Check all sensors and report status
    pub fn status_report(&self) -> String {
        let accel_status = self
            .accel
            .lock()
            .ok()
            .map(|s| s.status())
            .unwrap_or_else(|| "Accel: UNKNOWN".to_string());

        let gyro_status = self
            .gyro
            .lock()
            .ok()
            .map(|s| s.status())
            .unwrap_or_else(|| "Gyro: UNKNOWN".to_string());

        let gps_status = self
            .gps
            .lock()
            .ok()
            .map(|s| s.status())
            .unwrap_or_else(|| "GPS: UNKNOWN".to_string());

        format!("{} | {} | {}", accel_status, gyro_status, gps_status)
    }

    /// Signal restart for a sensor
    pub fn signal_accel_restart(&self) {
        if let Ok(mut state) = self.accel.lock() {
            if !state.restart_needed {
                eprintln!("[RESTART] Signaling Accel restart");
                state.signal_restart();
            }
        }
    }

    pub fn signal_gyro_restart(&self) {
        if let Ok(mut state) = self.gyro.lock() {
            if !state.restart_needed {
                eprintln!("[RESTART] Signaling Gyro restart");
                state.signal_restart();
            }
        }
    }

    pub fn signal_gps_restart(&self) {
        if let Ok(mut state) = self.gps.lock() {
            if !state.restart_needed {
                eprintln!("[RESTART] Signaling GPS restart");
                state.signal_restart();
            }
        }
    }

    /// Check if accel restart is ready
    pub fn accel_ready_restart(&self) -> bool {
        self.accel
            .lock()
            .ok()
            .map(|s| s.can_retry() && s.can_restart() && !s.circuit_tripped())
            .unwrap_or(false)
    }

    pub fn gyro_ready_restart(&self) -> bool {
        self.gyro
            .lock()
            .ok()
            .map(|s| s.can_retry() && s.can_restart() && !s.circuit_tripped())
            .unwrap_or(false)
    }

    pub fn gps_ready_restart(&self) -> bool {
        self.gps
            .lock()
            .ok()
            .map(|s| s.can_retry() && s.can_restart() && !s.circuit_tripped())
            .unwrap_or(false)
    }

    /// Record successful restart
    pub fn accel_restart_success(&self) {
        if let Ok(mut state) = self.accel.lock() {
            state.record_success();
        }
    }

    pub fn gyro_restart_success(&self) {
        if let Ok(mut state) = self.gyro.lock() {
            state.record_success();
        }
    }

    pub fn gps_restart_success(&self) {
        if let Ok(mut state) = self.gps.lock() {
            state.record_success();
        }
    }

    /// Record failed restart
    pub fn accel_restart_failed(&self) {
        if let Ok(mut state) = self.accel.lock() {
            state.record_failed_attempt();
        }
    }

    pub fn gyro_restart_failed(&self) {
        if let Ok(mut state) = self.gyro.lock() {
            state.record_failed_attempt();
        }
    }

    pub fn gps_restart_failed(&self) {
        if let Ok(mut state) = self.gps.lock() {
            state.record_failed_attempt();
        }
    }

    pub fn any_circuit_tripped(&self) -> bool {
        self.accel_circuit_tripped() || self.gyro_circuit_tripped() || self.gps_circuit_tripped()
    }

    pub fn accel_circuit_tripped(&self) -> bool {
        self.accel.lock().ok().map(|s| s.circuit_tripped()).unwrap_or(false)
    }

    pub fn gyro_circuit_tripped(&self) -> bool {
        self.gyro.lock().ok().map(|s| s.circuit_tripped()).unwrap_or(false)
    }

    pub fn gps_circuit_tripped(&self) -> bool {
        self.gps.lock().ok().map(|s| s.circuit_tripped()).unwrap_or(false)
    }
}

impl Default for RestartManager {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn test_restart_state_exponential_backoff() {
        let mut state = RestartState::new("test", 5, 1);

        state.signal_restart();
        assert!(state.restart_needed);
        assert!(state.can_retry());

        // First attempt: 1s cooldown
        state.record_failed_attempt();
        assert_eq!(state.attempts, 1);
        let cooldown1 = state.current_cooldown.as_secs_f64();
        assert!(cooldown1 >= 1.0 && cooldown1 < 2.0);

        // Second attempt: ~1.5s cooldown
        state.record_failed_attempt();
        let cooldown2 = state.current_cooldown.as_secs_f64();
        assert!(cooldown2 > cooldown1);
    }

    #[test]
    fn test_restart_state_max_attempts() {
        let mut state = RestartState::new("test", 2, 1);

        state.signal_restart();
        assert!(state.can_restart());

        state.record_failed_attempt();
        assert!(state.can_restart());

        state.record_failed_attempt();
        assert!(!state.can_restart());
    }

    #[test]
    fn test_circuit_breaker_trips() {
        let mut state = RestartState::new("test", 10, 1);
        state.signal_restart();

        for _ in 0..CIRCUIT_BREAKER_FAILS {
            state.record_failed_attempt();
        }

        assert!(state.circuit_tripped());
        assert!(!state.restart_needed);
    }

    #[test]
    fn test_restart_manager() {
        let manager = RestartManager::new();

        manager.signal_accel_restart();
        assert!(manager.accel_ready_restart());

        manager.accel_restart_success();
        let state = manager.accel.lock().unwrap();
        assert!(!state.restart_needed);
        assert_eq!(state.attempts, 0);
    }
}
