#!/usr/bin/env python3
"""
CYTHON INTEGRATION GUIDE FOR MOTION TRACKER V2

This file shows the EXACT changes needed to integrate Cython.
Copy-paste friendly!

Two changes required in motion_tracker_v2.py:
1. Add import at the top (with other imports)
2. Modify start_threads() method
"""

# ============================================================================
# CHANGE #1: Add this IMPORT near the top of motion_tracker_v2.py
# (after other imports, before the SensorFusion class definition)
# ============================================================================

CODE_TO_ADD_AT_TOP = '''
# Try to import Cython-optimized accelerometer processor
try:
    from accel_processor import FastAccelProcessor
    HAS_CYTHON = True
except ImportError:
    HAS_CYTHON = False
'''

# ============================================================================
# CHANGE #2: Replace the entire start_threads() method
# ============================================================================

CODE_TO_REPLACE_IN_START_THREADS = '''
    def start_threads(self):
        """Start background sensor threads"""
        print("Starting background sensor threads...")

        # Start sensor daemon (single long-lived process for continuous streaming)
        # Calculate delay_ms from desired sample rate
        delay_ms = max(10, int(1000 / self.accel_sample_rate))
        self.sensor_daemon = SensorDaemon(sensor_type='accelerometer', delay_ms=delay_ms)
        if not self.sensor_daemon.start():
            print("⚠ Sensor daemon failed to start, continuing anyway...")

        # Start GPS thread
        self.gps_thread = GPSThread(self.gps_queue, self.stop_event)
        self.gps_thread.start()
        print("✓ GPS thread started")

        # Start accelerometer thread
        if HAS_CYTHON and self.sensor_daemon:
            # ✓ CYTHON VERSION - Better performance, less sample loss
            # First, do calibration using pure Python thread
            temp_accel_thread = AccelerometerThread(
                self.accel_queue,
                self.stop_event,
                self.sensor_daemon,
                sample_rate=self.accel_sample_rate
            )
            temp_accel_thread.calibrate()

            # Now use Cython processor with calibration bias
            self.accel_processor = FastAccelProcessor(
                self.sensor_daemon,
                self.accel_queue,
                temp_accel_thread.bias,
                self.stop_event
            )

            # Start Cython processor in a thread
            self.accel_thread = threading.Thread(
                target=self.accel_processor.run,
                daemon=True
            )
            self.accel_thread.start()
            print(f"✓ Accelerometer thread started ({self.accel_sample_rate} Hz) [CYTHON]")

        else:
            # ⚠ PURE PYTHON VERSION - Fallback if Cython unavailable
            self.accel_thread = AccelerometerThread(
                self.accel_queue,
                self.stop_event,
                self.sensor_daemon,
                sample_rate=self.accel_sample_rate
            )
            self.accel_thread.start()
            print(f"✓ Accelerometer thread started ({self.accel_sample_rate} Hz)")
'''

# ============================================================================
# STEP-BY-STEP INSTRUCTIONS
# ============================================================================

"""
STEP 1: Edit motion_tracker_v2.py

Add this import block (after line ~15, with other imports):
--------
try:
    from accel_processor import FastAccelProcessor
    HAS_CYTHON = True
except ImportError:
    HAS_CYTHON = False
--------

STEP 2: Find the start_threads() method (around line 491)

Replace the entire method with the CODE_TO_REPLACE_IN_START_THREADS above.

STEP 3: Save the file

STEP 4: Test it!

python motion_tracker_v2.py 1

You should see:
  ✓ Sensor daemon started...
  ✓ GPS thread started
  ✓ Accelerometer thread started (50 Hz) [CYTHON]  <-- CYTHON indicator
  ✓ Calibrated...
"""

if __name__ == '__main__':
    print(__doc__)
    print("\n" + "="*80)
    print("IMPORT TO ADD (copy this):")
    print("="*80)
    print(CODE_TO_ADD_AT_TOP)
    print("\n" + "="*80)
    print("METHOD TO REPLACE (copy this):")
    print("="*80)
    print(CODE_TO_REPLACE_IN_START_THREADS)
