#!/usr/bin/env python3
"""
Standalone Accelerometer Validation Script

Run this to validate your accelerometer WITHOUT starting full tracking.
Useful for debugging sensor issues.

Usage:
    python validate_accel_only.py
"""

import subprocess
import json
import time
import math
import sys
import threading
from queue import Queue, Empty
from statistics import mean, stdev
from collections import deque

# Try to import health monitor
try:
    from accel_health_monitor import AccelHealthMonitor
    HAS_HEALTH_MONITOR = True
except ImportError:
    HAS_HEALTH_MONITOR = False
    print("⚠ AccelHealthMonitor not available (skipping detailed diagnostics)")

# Import the actual SensorDaemon from motion_tracker_v2
try:
    from motion_tracker_v2 import SensorDaemon
    HAS_SENSOR_DAEMON = True
except ImportError:
    HAS_SENSOR_DAEMON = False


class SimpleSensorDaemon:
    """Minimal sensor daemon for validation (fallback if import fails)"""

    def __init__(self, delay_ms=20):
        self.delay_ms = delay_ms
        self.process = None
        self.data_queue = Queue(maxsize=1000)

    def start(self):
        try:
            # Use stdbuf for line-buffered output
            cmd = f"stdbuf -oL termux-sensor -s 'lsm6dso LSM6DSO Accelerometer Non-wakeup' -d {self.delay_ms}"
            self.process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            # Start reader thread
            self.reader_thread = threading.Thread(target=self._read_stream, daemon=True)
            self.reader_thread.start()

            print(f"✓ Sensor daemon started ({1000//self.delay_ms}Hz)")
            return True
        except Exception as e:
            print(f"✗ Failed to start daemon: {e}")
            return False

    def _read_stream(self):
        """Read and parse continuous JSON stream"""
        try:
            json_buffer = ""
            brace_count = 0

            for line in self.process.stdout:
                json_buffer += line
                brace_count += line.count('{') - line.count('}')

                if brace_count == 0 and json_buffer.strip():
                    try:
                        data = json.loads(json_buffer)
                        for sensor_key, sensor_data in data.items():
                            if isinstance(sensor_data, dict) and 'values' in sensor_data:
                                values = sensor_data['values']
                                if len(values) >= 3:
                                    sample = {
                                        'x': values[0],
                                        'y': values[1],
                                        'z': values[2],
                                        'timestamp': time.time()
                                    }
                                    try:
                                        self.data_queue.put_nowait(sample)
                                    except:
                                        pass
                        json_buffer = ""
                    except:
                        json_buffer = ""
        except:
            pass

    def read_samples(self, duration=5):
        """Read raw samples for duration seconds"""
        samples = []
        start = time.time()

        while time.time() - start < duration:
            try:
                sample = self.data_queue.get(timeout=0.1)
                if sample:
                    samples.append(sample)
            except Empty:
                print(".", end="", flush=True)

        return samples

    def get_data(self, timeout=None):
        """Get next sensor reading"""
        try:
            return self.data_queue.get(timeout=timeout)
        except Empty:
            return None

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                self.process.kill()


def print_section(title):
    """Print formatted section header"""
    print("\n" + "="*80)
    print(title)
    print("="*80)


def validate_daemon():
    """Validate sensor daemon can read data"""
    print_section("STEP 1: DAEMON VALIDATION")

    # Use the real SensorDaemon if available (auto-detects sensor name)
    if HAS_SENSOR_DAEMON:
        daemon = SensorDaemon(sensor_type='accelerometer', delay_ms=20)
        if not daemon.start():
            print("✗ FAILED: Cannot start sensor daemon")
            print("\nFix:")
            print("  1. Check if Termux has Sensor permission:")
            print("     Settings → Permissions → Sensors → ALLOW")
            print("  2. Verify sensor exists:")
            print("     termux-sensor -l | grep -i accelerometer")
            return None, False

        # Wait longer for daemon to fully start
        time.sleep(2)

        print("Collecting samples for 5 seconds...", end="", flush=True)
        samples = []
        start = time.time()
        while time.time() - start < 5:
            sample = daemon.get_data(timeout=0.1)
            if sample:
                samples.append(sample)
                print("✓", end="", flush=True)
            else:
                print(".", end="", flush=True)
        daemon.stop()

        # Clean up processes
        subprocess.run(['pkill', '-9', 'termux-sensor'], check=False, capture_output=True)
        subprocess.run(['pkill', '-9', 'stdbuf'], check=False, capture_output=True)
    else:
        # Fallback to SimpleSensorDaemon
        daemon = SimpleSensorDaemon(delay_ms=20)
        if not daemon.start():
            print("✗ FAILED: Cannot start sensor daemon")
            print("\nFix:")
            print("  1. Check if Termux has Sensor permission:")
            print("     Settings → Permissions → Sensors → ALLOW")
            print("  2. Verify sensor exists:")
            print("     termux-sensor -l | grep -i accelerometer")
            return None, False

        time.sleep(1)

        print("Collecting samples for 5 seconds...", end="", flush=True)
        samples = daemon.read_samples(duration=5)
        daemon.stop()

    if not samples:
        print("\n✗ FAILED: No samples collected")
        print("\nTroubleshoot:")
        print("  1. Check sensor permissions")
        print("  2. Try: termux-sensor -s 'lsm6dso LSM6DSO Accelerometer Non-wakeup'")
        return None, False

    print(f" {len(samples)} samples")

    # Analyze samples
    magnitudes = [math.sqrt(s['x']**2 + s['y']**2 + s['z']**2) for s in samples]
    mag_mean = mean(magnitudes)
    mag_stdev = stdev(magnitudes) if len(magnitudes) > 1 else 0
    mag_min = min(magnitudes)
    mag_max = max(magnitudes)

    rate = len(samples) / 5.0

    print(f"\nResults:")
    print(f"  Samples collected: {len(samples)}")
    print(f"  Sample rate: {rate:.1f} Hz (target 50 Hz)")
    print(f"  Magnitude mean: {mag_mean:.2f} m/s²")
    print(f"  Magnitude range: {mag_min:.2f} - {mag_max:.2f} m/s²")
    print(f"  Magnitude stdev: {mag_stdev:.4f} m/s²")

    # Check values
    issues = []
    if len(samples) < 200:
        issues.append(f"Too few samples ({len(samples)}, expected ~250)")
    if rate < 40 or rate > 60:
        issues.append(f"Sample rate {rate:.1f}Hz outside 40-60Hz range")
    if mag_mean < 8 or mag_mean > 12:
        issues.append(f"Mean magnitude {mag_mean:.2f} outside 8-12m/s² range")

    if issues:
        print(f"\n⚠ WARNINGS:")
        for issue in issues:
            print(f"  • {issue}")
        return samples, False
    else:
        print(f"\n✓ DAEMON VALIDATION PASSED")
        return samples, True


def validate_calibration(samples):
    """Validate calibration"""
    print_section("STEP 2: CALIBRATION")

    if not samples:
        print("✗ Skipped (no samples from step 1)")
        return False

    # Get first 10 samples as calibration
    cal_samples = samples[:10]

    bias_x = mean(s['x'] for s in cal_samples)
    bias_y = mean(s['y'] for s in cal_samples)
    bias_z = mean(s['z'] for s in cal_samples)
    gravity = math.sqrt(bias_x**2 + bias_y**2 + bias_z**2)

    print(f"Calibration from first 10 samples:")
    print(f"  Bias X: {bias_x:.3f} m/s²")
    print(f"  Bias Y: {bias_y:.3f} m/s²")
    print(f"  Bias Z: {bias_z:.3f} m/s²")
    print(f"  Gravity magnitude: {gravity:.3f} m/s²")

    issues = []
    if gravity < 9.5:
        issues.append(f"Gravity too low ({gravity:.2f}m/s², expected 9.5-10.1)")
    elif gravity > 10.1:
        issues.append(f"Gravity too high ({gravity:.2f}m/s², expected 9.5-10.1)")

    if abs(bias_x) > 15 or abs(bias_y) > 15 or abs(bias_z) > 15:
        issues.append("One or more biases suspiciously large (possible sensor damage)")

    if issues:
        print(f"\n✗ WARNINGS:")
        for issue in issues:
            print(f"  • {issue}")

        print(f"\nIf during calibration you were:")
        print(f"  ✓ Keeping phone still → Sensor may be damaged")
        print(f"  ✗ Moving phone → Redo with phone perfectly still")
        return False
    else:
        print(f"\n✓ CALIBRATION VALIDATION PASSED")
        return True


def validate_motion_extraction(samples, gravity):
    """Test magnitude-based acceleration extraction"""
    print_section("STEP 3: ACCELERATION EXTRACTION (Magnitude-Based)")

    if not samples or not gravity:
        print("✗ Skipped (missing samples or gravity)")
        return

    print("Testing orientation-independent acceleration extraction...")
    print(f"\nUsing gravity magnitude: {gravity:.3f} m/s²\n")

    # Show first 10 samples and how they'd be processed
    print(f"{'#':<3} {'X':<8} {'Y':<8} {'Z':<8} {'Mag':<8} {'Motion':<8} {'Valid?':<8}")
    print("-" * 60)

    valid_count = 0
    for i, sample in enumerate(samples[:10]):
        x = sample['x']
        y = sample['y']
        z = sample['z']
        mag = math.sqrt(x**2 + y**2 + z**2)
        motion = max(0, mag - gravity)
        valid = (motion >= 0 and not math.isnan(motion))
        valid_count += valid

        print(f"{i:<3} {x:<8.2f} {y:<8.2f} {z:<8.2f} {mag:<8.2f} {motion:<8.2f} {'✓' if valid else '✗':<8}")

    print(f"\nResult: {valid_count}/10 samples extracted correctly")
    if valid_count == 10:
        print("✓ ACCELERATION EXTRACTION PASSED")
    else:
        print(f"⚠ {10-valid_count} samples had issues")


def validate_with_health_monitor(samples):
    """Use health monitor if available"""
    if not HAS_HEALTH_MONITOR:
        return

    print_section("STEP 4: HEALTH MONITOR VALIDATION (if available)")

    health = AccelHealthMonitor(target_sample_rate=50)

    # Simulate calibration
    if len(samples) >= 10:
        cal_samples = samples[:10]
        bias_x = mean(s['x'] for s in cal_samples)
        bias_y = mean(s['y'] for s in cal_samples)
        bias_z = mean(s['z'] for s in cal_samples)
        gravity = math.sqrt(bias_x**2 + bias_y**2 + bias_z**2)

        valid, gravity, issues = health.validate_calibration(bias_x, bias_y, bias_z)
        print(f"Health monitor calibration validation: {'✓ PASS' if valid else '✗ FAIL'}")
        if issues:
            print("Issues found:")
            for issue in issues:
                print(f"  • {issue}")

        # Check data quality
        quality_scores = []
        for sample in samples:
            quality, issues = health.check_data_quality(sample)
            quality_scores.append(quality)

        avg_quality = mean(quality_scores) if quality_scores else 0
        print(f"\nAverage data quality score: {avg_quality:.2f}/1.0")

        if avg_quality >= 0.9:
            print("✓ HEALTH CHECK PASSED")
        else:
            print(f"⚠ Quality below threshold ({avg_quality:.2f})")


def main():
    print("\n" + "="*80)
    print("STANDALONE ACCELEROMETER VALIDATION")
    print("="*80)
    print("\nThis tool validates your accelerometer setup")
    print("without running the full motion tracker.\n")

    # Step 1: Daemon
    samples, daemon_ok = validate_daemon()

    if not daemon_ok:
        print("\n" + "="*80)
        print("VALIDATION FAILED AT DAEMON STEP")
        print("Fix the issues above and retry.")
        print("="*80 + "\n")
        return

    # Step 2: Calibration (assume samples still)
    print("\n⚠ Using initial samples for calibration test")
    print("(In real tracking, you'd keep device still for 10 samples)")

    if len(samples) >= 10:
        cal_samples = samples[:10]
        bias_x = mean(s['x'] for s in cal_samples)
        bias_y = mean(s['y'] for s in cal_samples)
        bias_z = mean(s['z'] for s in cal_samples)
        gravity = math.sqrt(bias_x**2 + bias_y**2 + bias_z**2)

        cal_ok = validate_calibration(samples)
    else:
        print("Not enough samples for calibration test")
        cal_ok = False
        gravity = 9.81

    # Step 3: Acceleration extraction
    if len(samples) >= 10:
        validate_motion_extraction(samples, gravity)

    # Step 4: Health monitor
    validate_with_health_monitor(samples)

    # Summary
    print_section("VALIDATION SUMMARY")

    if daemon_ok and cal_ok:
        print("✓ ALL TESTS PASSED")
        print("\nYour accelerometer is ready for motion tracking!")
        print("Run: python motion_tracker_v2.py")
    else:
        print("⚠ SOME TESTS FAILED")
        print("\nReview the issues above before running motion tracking.")

    print("="*80 + "\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user\n")
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Error: {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)
