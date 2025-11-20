use std::collections::VecDeque;

/// Hann-window smoothing for accelerometer magnitudes
/// Matches Python motion_tracker_v2 pipeline for feature parity
pub struct AccelSmoother {
    window: VecDeque<f64>,
    window_size: usize,
    weights_cache: std::collections::HashMap<usize, Vec<f64>>,
}

impl AccelSmoother {
    /// Create a new smoother with given window size (typically 9)
    pub fn new(window_size: usize) -> Self {
        AccelSmoother {
            window: VecDeque::with_capacity(window_size),
            window_size,
            weights_cache: std::collections::HashMap::new(),
        }
    }

    /// Apply Hann-window smoothing to a magnitude value
    /// Returns the smoothed value
    pub fn apply(&mut self, magnitude: f64) -> f64 {
        self.window.push_back(magnitude);

        // Trim window if needed (shouldn't happen with VecDeque maxlen but be safe)
        while self.window.len() > self.window_size {
            self.window.pop_front();
        }

        let length = self.window.len();

        // Short windows: return directly or average [0.5, 0.5]
        if length == 1 {
            return magnitude;
        }

        // Get or compute Hann weights for this length
        let weights = if let Some(w) = self.weights_cache.get(&length) {
            w.clone()
        } else {
            let w = Self::compute_hann_weights(length);
            self.weights_cache.insert(length, w.clone());
            w
        };

        // Apply weighted average
        let mut smoothed = 0.0;
        for (value, weight) in self.window.iter().zip(weights.iter()) {
            smoothed += value * weight;
        }

        smoothed
    }

    /// Compute Hann weights for a given length
    /// Matches Python: 0.5 - 0.5 * cos(2π*i / (length-1))
    fn compute_hann_weights(length: usize) -> Vec<f64> {
        if length <= 1 {
            return vec![1.0];
        }
        if length == 2 {
            return vec![0.5, 0.5];
        }

        let mut weights = Vec::with_capacity(length);
        for i in 0..length {
            let angle = (2.0 * std::f64::consts::PI * i as f64) / (length as f64 - 1.0);
            let w = 0.5 - 0.5 * angle.cos();
            weights.push(w);
        }

        // Normalize
        let sum: f64 = weights.iter().sum();
        let total = if sum > 0.0 { sum } else { 1.0 };
        weights.iter_mut().for_each(|w| *w /= total);

        weights
    }

    /// Get current window size (actual, not max)
    pub fn len(&self) -> usize {
        self.window.len()
    }

    /// Check if window is empty
    pub fn is_empty(&self) -> bool {
        self.window.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_single_value() {
        let mut smoother = AccelSmoother::new(9);
        let result = smoother.apply(5.0);
        assert_eq!(result, 5.0);
    }

    #[test]
    fn test_two_values() {
        let mut smoother = AccelSmoother::new(9);
        smoother.apply(2.0);
        let result = smoother.apply(4.0);
        // [0.5, 0.5] weighted: 2.0*0.5 + 4.0*0.5 = 3.0
        assert!((result - 3.0).abs() < 0.001);
    }

    #[test]
    fn test_window_accumulation() {
        let mut smoother = AccelSmoother::new(3);
        smoother.apply(1.0);
        smoother.apply(2.0);
        let result = smoother.apply(3.0);
        // Window: [1.0, 2.0, 3.0]
        // Hann weights for length 3 should be normalized
        assert!(result > 1.0 && result < 3.0);
    }

    #[test]
    fn test_window_wrapping() {
        let mut smoother = AccelSmoother::new(2);
        smoother.apply(1.0);
        smoother.apply(2.0);
        let result = smoother.apply(3.0); // Should drop 1.0, keep [2.0, 3.0]
        assert_eq!(smoother.len(), 2);
        // [2.0, 3.0] with [0.5, 0.5] = 2.5
        assert!((result - 2.5).abs() < 0.001);
    }

    #[test]
    fn test_weights_cache() {
        let mut smoother = AccelSmoother::new(9);
        // Fill window to length 5
        for i in 1..=5 {
            smoother.apply(i as f64);
        }
        let cache_size_before = smoother.weights_cache.len();

        // Apply more values - length 9 should be cached
        for i in 6..=9 {
            smoother.apply(i as f64);
        }

        assert!(smoother.weights_cache.len() >= cache_size_before);
    }
}
