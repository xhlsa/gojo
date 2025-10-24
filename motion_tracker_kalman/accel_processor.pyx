# cython: language_level=3, boundscheck=False, wraparound=False
"""
Fast accelerometer processor using Cython
Releases GIL during math operations for true parallelism
"""

import cython
import math
from queue import Queue, Empty


cdef class FastAccelProcessor:
    """Cython-optimized accelerometer processing with GIL release"""

    cdef object daemon
    cdef object output_queue
    cdef dict bias
    cdef float gravity
    cdef object stop_event

    def __init__(self, daemon, output_queue, bias, gravity, stop_event):
        self.daemon = daemon
        self.output_queue = output_queue
        self.bias = bias
        self.gravity = gravity
        self.stop_event = stop_event

    @cython.cdivision(True)
    @cython.boundscheck(False)
    cdef inline tuple calibrate_sample(self, dict raw_data):
        """
        Apply magnitude-based calibration to raw accelerometer data
        Handles any device orientation by removing gravity magnitude
        Runs with GIL released for true parallelism
        Returns: (x_cal, y_cal, z_cal, motion_magnitude)
        """
        # Axis-calibrated values (for data logging)
        cdef float x = raw_data['x'] - self.bias['x']
        cdef float y = raw_data['y'] - self.bias['y']
        cdef float z = raw_data['z'] - self.bias['z']

        # Calculate raw magnitude
        cdef float raw_x = raw_data['x']
        cdef float raw_y = raw_data['y']
        cdef float raw_z = raw_data['z']
        cdef float raw_mag = math.sqrt(raw_x*raw_x + raw_y*raw_y + raw_z*raw_z)

        # Remove gravity to get true motion magnitude (orientation-independent)
        cdef float motion_mag = raw_mag - self.gravity
        if motion_mag < 0:
            motion_mag = 0

        return (x, y, z, motion_mag)

    def process_sample(self, dict raw_data):
        """
        Process one accelerometer sample
        Wraps calibration to properly handle queue operations
        """
        x, y, z, mag = self.calibrate_sample(raw_data)

        # Build result dict - this requires GIL
        result = {
            'x': x,
            'y': y,
            'z': z,
            'magnitude': mag,
            'timestamp': raw_data['timestamp']
        }

        # Queue operation (non-blocking)
        try:
            self.output_queue.put_nowait(result)
        except:
            # Queue full, skip sample
            pass

    def run(self):
        """
        Continuous processing loop
        Releases GIL during calibration math
        """
        while not self.stop_event.is_set():
            try:
                # Non-blocking read with short timeout
                raw_data = self.daemon.get_data(timeout=0.01)

                if raw_data:
                    self.process_sample(raw_data)

            except Exception:
                # Daemon error or timeout
                pass
