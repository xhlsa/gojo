#!/usr/bin/env python3
"""
Accelerometer Health Monitor - Validates sensor initialization and tracks data quality

Provides comprehensive diagnostics for accelerometer daemon and calibration.
Detects common failure modes early and reports detailed status.
"""

import time
import math
from collections import deque
from statistics import mean, stdev


class AccelHealthMonitor:
    """
    Validates accelerometer initialization, calibration, and runtime data quality.
    Catches sensor failures early before they corrupt tracking data.
    """

    def __init__(self, target_sample_rate=50):
        self.target_sample_rate = target_sample_rate

        # Startup validation
        self.startup_validated = False
        self.startup_samples_collected = 0
        self.startup_start_time = None

        # Calibration tracking
        self.calibration_data = {
            'samples': 0,
            'gravity_magnitude': None,
            'gravity_expected_min': 9.5,
            'gravity_expected_max': 10.1,
            'bias_x': None,
            'bias_y': None,
            'bias_z': None
        }

        # Runtime monitoring
        self.sample_timestamps = deque(maxlen=200)  # Last 200 timestamps for rate check
        self.gravity_magnitude_history = deque(maxlen=100)  # Track gravity over time
        self.last_gravity_check_time = None
        self.gravity_drift_threshold = 0.5  # m/s² - alert if changes more than this

        # Data quality metrics
        self.queue_stalls = 0
        self.last_sample_time = None
        self.stall_timeout = 2.0  # Warn if no sample for 2 seconds

        # Warnings and errors
        self.warnings = []
        self.errors = []
        self.diagnostics = {}

    def validate_startup(self, sensor_daemon, duration=5, target_samples=50):
        """
        Validate that sensor daemon is producing data.

        Args:
            sensor_daemon: SensorDaemon instance to test
            duration: How long to wait for samples (seconds)
            target_samples: How many samples we expect to collect

        Returns:
            (success: bool, sample_count: int, rate_hz: float)
        """
        self.warnings.clear()
        self.errors.clear()
        self.startup_start_time = time.time()
        self.startup_samples_collected = 0

        print("\n" + "="*80)
        print("ACCELEROMETER STARTUP VALIDATION")
        print("="*80)
        print(f"Testing daemon for {duration}s, expecting ~{target_samples} samples at {self.target_sample_rate}Hz...")

        start_time = time.time()
        samples = []

        # Collect samples
        while time.time() - start_time < duration:
            data = sensor_daemon.get_data(timeout=0.5)
            if data:
                samples.append(data)
                print(".", end="", flush=True)
            else:
                print("✗", end="", flush=True)

        print()

        self.startup_samples_collected = len(samples)

        if len(samples) == 0:
            self.errors.append("NO SAMPLES COLLECTED - Sensor daemon is not reading data")
            print(f"✗ FAILED: No accelerometer samples received")
            return False, 0, 0.0

        # Calculate actual sample rate
        if len(samples) > 1:
            time_span = samples[-1]['timestamp'] - samples[0]['timestamp']
            actual_rate = len(samples) / time_span if time_span > 0 else 0
        else:
            actual_rate = 0

        print(f"✓ Collected {len(samples)} samples in {duration}s")
        print(f"  Actual rate: {actual_rate:.1f} Hz (target: {self.target_sample_rate} Hz)")

        # Validate sample rate
        expected_min = self.target_sample_rate * 0.8  # Allow 20% variance
        expected_max = self.target_sample_rate * 1.2

        if actual_rate < expected_min or actual_rate > expected_max:
            self.warnings.append(
                f"Sample rate {actual_rate:.1f}Hz outside expected range "
                f"({expected_min:.1f}-{expected_max:.1f}Hz)"
            )
            print(f"⚠ WARNING: Sample rate {actual_rate:.1f}Hz is outside expected range")
        else:
            print(f"✓ Sample rate is within acceptable range")

        # Check for sample regularity
        if len(samples) > 2:
            deltas = [
                samples[i+1]['timestamp'] - samples[i]['timestamp']
                for i in range(len(samples)-1)
            ]
            delta_mean = mean(deltas)
            delta_stdev = stdev(deltas) if len(deltas) > 1 else 0

            print(f"  Sample interval: {delta_mean*1000:.1f}ms ±{delta_stdev*1000:.1f}ms")

            if delta_stdev > delta_mean * 0.3:  # > 30% jitter
                self.warnings.append(f"High timing jitter in samples ({delta_stdev*1000:.1f}ms stdev)")
                print(f"⚠ WARNING: High timing jitter detected")

        # Check value ranges
        magnitudes = [
            math.sqrt(s['x']**2 + s['y']**2 + s['z']**2) for s in samples
        ]
        mag_mean = mean(magnitudes)
        mag_min = min(magnitudes)
        mag_max = max(magnitudes)

        print(f"  Magnitude range: {mag_min:.2f} to {mag_max:.2f} m/s²")
        print(f"  Mean magnitude: {mag_mean:.2f} m/s²")

        # Magnitude should be around 9.81 (gravity) if device is still
        if mag_mean < 8.0 or mag_mean > 12.0:
            self.warnings.append(
                f"Mean magnitude {mag_mean:.2f}m/s² seems unusual "
                f"(expect ~9.81 if stationary)"
            )
            print(f"⚠ WARNING: Mean magnitude {mag_mean:.2f}m/s² outside expected range for still device")

        self.startup_validated = True
        self.diagnostics['startup'] = {
            'samples_collected': len(samples),
            'actual_rate_hz': actual_rate,
            'magnitude_mean': mag_mean,
            'magnitude_range': (mag_min, mag_max)
        }

        if len(samples) >= int(target_samples * 0.8):
            print(f"✓ STARTUP VALIDATION PASSED\n")
            return True, len(samples), actual_rate
        else:
            self.errors.append(
                f"Too few samples: {len(samples)} collected, "
                f"{int(target_samples*0.8)} required"
            )
            print(f"✗ FAILED: Too few samples\n")
            return False, len(samples), actual_rate

    def validate_calibration(self, bias_x, bias_y, bias_z):
        """
        Validate that calibration makes physical sense.

        Args:
            bias_x, bias_y, bias_z: Calibration offsets from accelerometer thread

        Returns:
            (valid: bool, gravity_magnitude: float, issues: list)
        """
        print("\n" + "="*80)
        print("CALIBRATION VALIDATION")
        print("="*80)

        self.calibration_data['bias_x'] = bias_x
        self.calibration_data['bias_y'] = bias_y
        self.calibration_data['bias_z'] = bias_z

        gravity = math.sqrt(bias_x**2 + bias_y**2 + bias_z**2)
        self.calibration_data['gravity_magnitude'] = gravity

        print(f"Calibration biases:")
        print(f"  X: {bias_x:+.3f} m/s²")
        print(f"  Y: {bias_y:+.3f} m/s²")
        print(f"  Z: {bias_z:+.3f} m/s²")
        print(f"  Gravity magnitude: {gravity:.3f} m/s²")

        issues = []

        # Check if gravity magnitude is reasonable
        if gravity < self.calibration_data['gravity_expected_min']:
            issues.append(
                f"Gravity magnitude {gravity:.2f}m/s² is BELOW expected range "
                f"(< {self.calibration_data['gravity_expected_min']}m/s²)"
            )
            print(f"✗ ERROR: Gravity magnitude too low ({gravity:.2f}m/s²)")
        elif gravity > self.calibration_data['gravity_expected_max']:
            issues.append(
                f"Gravity magnitude {gravity:.2f}m/s² is ABOVE expected range "
                f"(> {self.calibration_data['gravity_expected_max']}m/s²)"
            )
            print(f"✗ ERROR: Gravity magnitude too high ({gravity:.2f}m/s²)")
        else:
            print(f"✓ Gravity magnitude is valid")

        # Check if biases are reasonable (shouldn't exceed ±15 m/s²)
        for name, value in [('X', bias_x), ('Y', bias_y), ('Z', bias_z)]:
            if abs(value) > 15:
                issues.append(
                    f"Bias {name} is suspiciously large ({abs(value):.1f}m/s² - "
                    f"sensor may be broken)"
                )
                print(f"✗ ERROR: Bias {name} is very large ({abs(value):.1f}m/s²)")

        self.last_gravity_check_time = time.time()
        self.gravity_magnitude_history.append(gravity)

        valid = len(issues) == 0

        if valid:
            print(f"✓ CALIBRATION VALIDATION PASSED\n")
        else:
            print(f"✗ CALIBRATION VALIDATION FAILED\n")

        return valid, gravity, issues

    def check_data_quality(self, accel_data):
        """
        Check quality of incoming accelerometer data (call on each sample).

        Args:
            accel_data: Single accelerometer sample with timestamp

        Returns:
            quality_score (0.0-1.0), issues_found (list of warnings)
        """
        if accel_data is None:
            return 0.0, ["No data"]

        timestamp = accel_data.get('timestamp', time.time())
        issues = []

        # Track sample timing
        self.sample_timestamps.append(timestamp)
        self.last_sample_time = timestamp

        # Check for NaN or invalid values
        x, y, z = accel_data.get('x', 0), accel_data.get('y', 0), accel_data.get('z', 0)

        if not all(isinstance(v, (int, float)) for v in [x, y, z]):
            issues.append("Non-numeric accelerometer values")
            return 0.0, issues

        if any(math.isnan(v) or math.isinf(v) for v in [x, y, z]):
            issues.append("NaN or Inf in accelerometer values")
            return 0.0, issues

        # Check for physically unreasonable values (>100 m/s²)
        magnitude = math.sqrt(x**2 + y**2 + z**2)
        if magnitude > 100:
            issues.append(f"Unreasonable magnitude {magnitude:.1f}m/s² (>100)")
            return 0.5, issues

        # Check gravity magnitude drift
        if self.calibration_data['gravity_magnitude'] is not None:
            gravity_drift = abs(magnitude - self.calibration_data['gravity_magnitude'])
            self.gravity_magnitude_history.append(magnitude)

            if gravity_drift > self.gravity_drift_threshold:
                if self.last_gravity_check_time is None or \
                   (time.time() - self.last_gravity_check_time) > 30:
                    # Only alert once every 30 seconds
                    issues.append(
                        f"Gravity drift: {gravity_drift:.2f}m/s² from baseline"
                    )
                    self.last_gravity_check_time = time.time()

        # Calculate quality score
        quality = 1.0
        if issues:
            quality = 0.8

        return quality, issues

    def detect_queue_stall(self):
        """
        Check if accelerometer queue has stalled (no fresh data for too long).
        Call this periodically from main loop.

        Returns:
            (is_stalled: bool, time_since_last: float)
        """
        if self.last_sample_time is None:
            return False, 0.0

        time_since_last = time.time() - self.last_sample_time
        is_stalled = time_since_last > self.stall_timeout

        if is_stalled:
            self.queue_stalls += 1

        return is_stalled, time_since_last

    def get_sample_rate(self):
        """
        Calculate actual sample rate from recent timestamps.

        Returns:
            rate_hz: Current observed sample rate
        """
        if len(self.sample_timestamps) < 10:
            return 0.0

        time_span = self.sample_timestamps[-1] - self.sample_timestamps[0]
        if time_span <= 0:
            return 0.0

        return len(self.sample_timestamps) / time_span

    def get_diagnostics(self):
        """
        Get comprehensive diagnostic report.

        Returns:
            dict with all diagnostic data
        """
        rate = self.get_sample_rate()
        is_stalled, stall_time = self.detect_queue_stall()

        diagnostics = {
            'startup_validated': self.startup_validated,
            'startup_samples': self.startup_samples_collected,
            'current_sample_rate_hz': rate,
            'current_sample_rate_healthy': (
                rate > self.target_sample_rate * 0.8 and
                rate < self.target_sample_rate * 1.2
            ),
            'queue_stalled': is_stalled,
            'time_since_last_sample_ms': stall_time * 1000,
            'gravity_magnitude': (
                self.calibration_data['gravity_magnitude'] or 'Not calibrated'
            ),
            'gravity_drift_detected': (
                len(self.gravity_magnitude_history) > 0 and
                self.calibration_data['gravity_magnitude'] is not None and
                abs(self.gravity_magnitude_history[-1] -
                    self.calibration_data['gravity_magnitude']) > self.gravity_drift_threshold
            ),
            'queue_stall_count': self.queue_stalls,
            'warnings': self.warnings,
            'errors': self.errors
        }

        return diagnostics

    def print_diagnostics(self):
        """Print formatted diagnostic report to console."""
        diag = self.get_diagnostics()

        print("\n" + "="*80)
        print("ACCELEROMETER HEALTH DIAGNOSTICS")
        print("="*80)

        print(f"\nInitialization:")
        print(f"  Startup validated: {'✓ Yes' if diag['startup_validated'] else '✗ No'}")
        print(f"  Startup samples: {diag['startup_samples']}")

        print(f"\nCalibration:")
        print(f"  Gravity magnitude: {diag['gravity_magnitude']}")

        print(f"\nRuntime:")
        print(f"  Sample rate: {diag['current_sample_rate_hz']:.1f} Hz "
              f"({'✓ Healthy' if diag['current_sample_rate_healthy'] else '⚠ Out of range'})")
        print(f"  Queue status: {'✓ Active' if not diag['queue_stalled'] else '✗ STALLED'}")
        print(f"  Time since last sample: {diag['time_since_last_sample_ms']:.0f}ms")
        print(f"  Gravity drift detected: {'⚠ Yes' if diag['gravity_drift_detected'] else '✓ No'}")
        print(f"  Queue stall count: {diag['queue_stall_count']}")

        if diag['errors']:
            print(f"\nErrors ({len(diag['errors'])}):")
            for error in diag['errors']:
                print(f"  ✗ {error}")

        if diag['warnings']:
            print(f"\nWarnings ({len(diag['warnings'])}):")
            for warning in diag['warnings']:
                print(f"  ⚠ {warning}")

        if not diag['errors'] and not diag['warnings']:
            print(f"\n✓ No errors or warnings")

        print("="*80)
