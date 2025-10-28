"""
GYROSCOPE INTEGRATION CODE FOR MOTION_TRACKER_V2.PY

This file contains production-ready code for integrating gyroscope support
into motion_tracker_v2.py. Three components are included:

A) PersistentGyroDaemon class - Continuous gyroscope stream reading
B) AccelerometerThread modifications - Gyro data processing and rotation detection
C) MotionTrackerV2.start_threads() modifications - Initialization and threading

Insert these components into motion_tracker_v2.py following the exact instructions
in each section.

DESIGN PRINCIPLES:
- Mirrors existing PersistentAccelDaemon pattern
- Thread-safe with stop_event pattern
- Graceful fallback if gyroscope unavailable
- Comprehensive logging for debugging
- Brace-depth JSON parsing identical to accelerometer daemon
"""

# ============================================================================
# SECTION A: PersistentGyroDaemon CLASS
# ============================================================================
# INSERT AFTER: PersistentAccelDaemon class (around line 416)
# LOCATION: Between PersistentAccelDaemon.__del__ and GPSThread class definition

class PersistentGyroDaemon:
    """
    Persistent gyroscope daemon - starts termux-sensor ONCE and reads continuously.

    Mirrors PersistentAccelDaemon pattern:
    - Single long-lived termux-sensor process
    - Continuous JSON stream with brace-depth parsing
    - Queue-based data passing to AccelerometerThread
    - Thread-safe with stop_event

    The gyroscope provides angular velocity data (rad/s) that, when integrated,
    gives absolute rotation angles. Used by RotationDetector to detect device
    orientation changes that trigger accelerometer recalibration.
    """

    def __init__(self, delay_ms=50, max_queue_size=1000):
        """
        Initialize gyroscope daemon.

        Args:
            delay_ms (int): Polling delay in milliseconds (50ms = ~20Hz sampling)
            max_queue_size (int): Maximum queue depth before dropping samples
        """
        self.delay_ms = delay_ms
        self.data_queue = Queue(maxsize=max_queue_size)
        self.reader_thread = None
        self.stop_event = threading.Event()
        self.sensor_process = None

    def start(self):
        """Start persistent termux-sensor daemon for gyroscope"""
        try:
            # Start termux-sensor with same parameters as accelerometer
            # -s GYROSCOPE: Select gyroscope sensor
            # -d 50: 50ms polling delay for ~20Hz hardware rate
            # stdbuf -oL: Line-buffered output (one JSON object per line)
            self.sensor_process = subprocess.Popen(
                ['stdbuf', '-oL', 'termux-sensor', '-s', 'GYROSCOPE', '-d', '50'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1  # Line buffered
            )

            # Verify process started
            if self.sensor_process.poll() is not None:
                stderr_out = self.sensor_process.stderr.read() if self.sensor_process.stderr else ""
                raise RuntimeError(f"termux-sensor GYROSCOPE exited immediately: {stderr_out}")

            # Start reader thread
            self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.reader_thread.start()

            delay_hz = 1000 // self.delay_ms
            print(f"✓ Gyroscope daemon started ({delay_hz:.0f}Hz, persistent stream)")
            print(f"   Process: termux-sensor (PID {self.sensor_process.pid})")
            return True
        except Exception as e:
            print(f"⚠ Failed to start gyroscope daemon: {e}")
            return False

    def _read_loop(self):
        """Read JSON objects from persistent gyroscope stream (multi-line formatted)"""
        try:
            if not self.sensor_process or not self.sensor_process.stdout:
                print("⚠ [GyroDaemon] No stdout from sensor process", file=sys.stderr)
                return

            json_buffer = ""
            brace_depth = 0
            line_count = 0

            for line in self.sensor_process.stdout:
                if self.stop_event.is_set():
                    break

                if not line:
                    continue

                line_count += 1
                json_buffer += line + '\n'

                # Track brace depth to detect complete JSON objects
                brace_depth += line.count('{') - line.count('}')

                # When brace_depth returns to 0, we have a complete JSON object
                if brace_depth == 0 and '{' in json_buffer and json_buffer.count('}') > 0:
                    try:
                        # Parse the complete JSON object
                        data = json_loads(json_buffer)
                        json_buffer = ""

                        # Extract gyroscope values from any sensor key
                        for sensor_key, sensor_data in data.items():
                            if isinstance(sensor_data, dict) and 'values' in sensor_data:
                                values = sensor_data['values']
                                if len(values) >= 3:
                                    gyro_data = {
                                        'x': values[0],  # rad/s around X-axis (pitch)
                                        'y': values[1],  # rad/s around Y-axis (roll)
                                        'z': values[2],  # rad/s around Z-axis (yaw)
                                        'timestamp': time.time()
                                    }

                                    # Try to put in queue, skip if full
                                    try:
                                        self.data_queue.put_nowait(gyro_data)
                                    except:
                                        # Queue full, skip this sample
                                        pass
                                    break  # Only process first sensor

                    except (ValueError, KeyError, IndexError, TypeError):
                        # Skip malformed JSON, continue buffering
                        json_buffer = ""
                        brace_depth = 0

        except Exception as e:
            print(f"⚠ [GyroDaemon] Reader thread error: {e}", file=sys.stderr)
        finally:
            # Clean up process
            if self.sensor_process:
                try:
                    self.sensor_process.terminate()
                    self.sensor_process.wait(timeout=1)
                except:
                    try:
                        self.sensor_process.kill()
                    except:
                        pass

    def stop(self):
        """Stop the daemon"""
        self.stop_event.set()

        # Kill sensor process if running
        if self.sensor_process:
            try:
                self.sensor_process.terminate()
                self.sensor_process.wait(timeout=1)
            except:
                try:
                    self.sensor_process.kill()
                except:
                    pass

        # Wait for reader thread
        if self.reader_thread:
            self.reader_thread.join(timeout=2)

    def __del__(self):
        """Ensure cleanup if daemon is garbage collected without explicit stop()"""
        try:
            self.stop()
        except:
            pass  # Silently ignore errors during cleanup

    def get_data(self, timeout=None):
        """Get next gyroscope reading from daemon"""
        try:
            return self.data_queue.get(timeout=timeout)
        except Empty:
            return None


# ============================================================================
# SECTION B: AccelerometerThread MODIFICATIONS
# ============================================================================
# MODIFY: AccelerometerThread.__init__ (around line 482)
# ADD TWO NEW PARAMETERS before the calibration section

# ORIGINAL CODE (line 482):
#     def __init__(self, accel_queue, stop_event, sensor_daemon, fusion=None, sample_rate=50, health_monitor=None):

# MODIFIED CODE - Replace with:
#     def __init__(self, accel_queue, stop_event, sensor_daemon, fusion=None, sample_rate=50,
#                  health_monitor=None, gyro_daemon=None, rotation_detector=None):

# Then ADD these lines after line 489 (after self.health_monitor = health_monitor):

#         # Gyroscope support (optional)
#         self.gyro_daemon = gyro_daemon
#         self.rotation_detector = rotation_detector
#
#         # Rotation detection thresholds
#         self.rotation_recal_threshold = 0.5  # radians (~28.6°)
#         self.last_rotation_recal_time = None
#         self.rotation_recal_interval = 5  # seconds - check every 5s
#         self.last_rotation_magnitude = 0.0


# ============================================================================
# MODIFICATION 2: AccelerometerThread.run() METHOD
# ============================================================================
# MODIFY: Inside the run() method, in the main while loop (around line 646)
# LOCATION: After the "raw_data = self.sensor_daemon.get_data(timeout=0.1)" block

# ADD THIS CODE BLOCK after line 676 (after accel_data = self.read_calibrated(raw_data)):

#         # GYROSCOPE PROCESSING - If daemon available, read and process gyro data
#         if self.gyro_daemon and self.rotation_detector:
#             try:
#                 # Non-blocking read from gyroscope daemon
#                 gyro_raw = self.gyro_daemon.get_data(timeout=0.01)
#
#                 if gyro_raw and accel_data:
#                     # Calculate dt since last accelerometer sample
#                     # Use accel_data timestamp as reference
#                     current_time = time.time()
#                     dt = current_time - (accel_data.get('timestamp', current_time) - 0.05)
#
#                     if dt > 0 and dt < 0.2:  # Valid dt range
#                         # Update rotation detector with gyroscope data
#                         self.rotation_detector.update_gyroscope(
#                             gyro_raw['x'],
#                             gyro_raw['y'],
#                             gyro_raw['z'],
#                             dt
#                         )
#
#                         # Check if rotation exceeded threshold (rotation detection)
#                         rotation_state = self.rotation_detector.get_rotation_state()
#                         total_rotation_rad = rotation_state['total_rotation_radians']
#
#                         current_time_check = time.time()
#
#                         # Trigger recalibration if significant rotation detected
#                         if total_rotation_rad > self.rotation_recal_threshold:
#                             if (self.last_rotation_recal_time is None or
#                                 (current_time_check - self.last_rotation_recal_time >= self.rotation_recal_interval)):
#
#                                 rotation_degrees = rotation_state['total_rotation_degrees']
#                                 primary_axis = rotation_state['primary_axis']
#
#                                 print(f"⚡ [Rotation] Detected {rotation_degrees:.1f}° rotation "
#                                      f"(axis: {primary_axis}, threshold: {math.degrees(self.rotation_recal_threshold):.1f}°)")
#                                 print(f"   Triggering accelerometer recalibration...")
#
#                                 # Perform recalibration
#                                 if self.fusion:
#                                     # Force recalibration by setting stationary=True
#                                     self.try_recalibrate(is_stationary=True)
#
#                                 # Reset rotation angles after recalibration
#                                 self.rotation_detector.reset_rotation_angles()
#                                 self.last_rotation_recal_time = current_time_check
#
#                                 print(f"   ✓ Recalibration complete, rotation angles reset")
#
#                         self.last_rotation_magnitude = total_rotation_rad
#
#             except Exception as e:
#                 # Log gyro errors but don't interrupt accel processing
#                 if not self.stop_event.is_set():
#                     # Only print on first error to avoid log spam
#                     if gyro_raw and hasattr(self, '_last_gyro_error_time'):
#                         if time.time() - self._last_gyro_error_time > 10:
#                             print(f"\n⚠ Gyro processing error (continuing): {e}")
#                             self._last_gyro_error_time = time.time()
#                     elif not hasattr(self, '_last_gyro_error_time'):
#                         self._last_gyro_error_time = time.time()


# ============================================================================
# SECTION C: MotionTrackerV2.start_threads() MODIFICATIONS
# ============================================================================
# MODIFY: MotionTrackerV2 class initialization (around line 723)
# ADD these instance variables in __init__ after line 749:

#         # Gyroscope daemon and rotation detector
#         self.gyro_daemon = None
#         self.rotation_detector = None


# ============================================================================
# MODIFICATION 2: MotionTrackerV2.start_threads() METHOD
# ============================================================================
# MODIFY: The start_threads() method (around line 896)
# ADD IMPORT at top of motion_tracker_v2.py (around line 20-25):

#     from rotation_detector import RotationDetector


# THEN in start_threads(), ADD THIS CODE after the accelerometer daemon startup
# (around line 910, after "time.sleep(2)" in the else block):

#         # Start gyroscope daemon (optional, non-critical)
#         print("Starting gyroscope daemon...")
#         try:
#             self.gyro_daemon = PersistentGyroDaemon(delay_ms=50)
#             if self.gyro_daemon.start():
#                 print("✓ Gyroscope daemon started")
#                 # Give daemon a moment to start producing data
#                 time.sleep(0.5)
#             else:
#                 print("⚠ Gyroscope daemon failed to start (rotation detection disabled)")
#                 self.gyro_daemon = None
#         except Exception as e:
#             print(f"⚠ Failed to initialize gyroscope daemon: {e}")
#             self.gyro_daemon = None


# THEN, MODIFY the AccelerometerThread initialization (around line 966-973).
# The original line:
#     self.accel_thread = AccelerometerThread(
#         self.accel_queue,
#         self.stop_event,
#         self.sensor_daemon,
#         fusion=self.fusion,
#         sample_rate=self.accel_sample_rate,
#         health_monitor=self.health_monitor
#     )

# CHANGE TO:
#     # Initialize rotation detector if gyro available
#     if self.gyro_daemon:
#         self.rotation_detector = RotationDetector(history_size=6000)
#         print(f"✓ RotationDetector initialized (history: 6000 samples)")
#
#     self.accel_thread = AccelerometerThread(
#         self.accel_queue,
#         self.stop_event,
#         self.sensor_daemon,
#         fusion=self.fusion,
#         sample_rate=self.accel_sample_rate,
#         health_monitor=self.health_monitor,
#         gyro_daemon=self.gyro_daemon,
#         rotation_detector=self.rotation_detector
#     )


# ============================================================================
# MODIFICATION 3: Cleanup in track() method
# ============================================================================
# MODIFY: The cleanup section in track() method (around line 1227-1232)
# ADD this code after the sensor_daemon.stop() block:

#         # Stop gyroscope daemon
#         if self.gyro_daemon:
#             try:
#                 self.gyro_daemon.stop()
#                 print("  ✓ Gyroscope daemon stopped")
#             except Exception as e:
#                 print(f"⚠ Error stopping gyroscope daemon: {e}")


# ============================================================================
# END OF INTEGRATION CODE
# ============================================================================
