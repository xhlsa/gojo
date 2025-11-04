#!/usr/bin/env python3
"""
Incident Detection Module

Detects and logs driving incidents:
- Hard braking (>0.8g deceleration)
- Impacts (>1.5g acceleration)
- Swerving (>60°/sec rotation)

Each incident is saved with 30 seconds of context before and after the event,
including GPS location, acceleration, and gyroscope data.

Usage:
    detector = IncidentDetector(session_dir='incidents/')
    detector.check_hard_braking(magnitude=0.85)  # Logs hard braking
    detector.check_impact(magnitude=1.6)         # Logs impact
    detector.check_swerving(angular_velocity=1.2) # Logs swerving (rad/s)
"""

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime


class IncidentDetector:
    """
    Detects and logs driving incidents with context windows.

    Each incident is saved with 30 seconds of pre-event and post-event data.
    """

    # Detection thresholds (in standard units)
    THRESHOLDS = {
        'hard_braking': 0.8,      # g-forces (m/s²/9.81)
        'impact': 1.5,            # g-forces
        'swerving': 1.047,        # radians/second (60°/s = 1.047 rad/s)
    }

    # Data context window
    CONTEXT_SECONDS = 30  # Keep 30 sec before and after event

    def __init__(self, session_dir='incidents/', sensor_sample_rate=50):
        """
        Initialize incident detector.

        Args:
            session_dir (str): Directory to save incident files
            sensor_sample_rate (int): Accel/gyro sampling rate (Hz)
        """
        self.session_dir = session_dir
        self.sensor_sample_rate = sensor_sample_rate

        # Create incidents directory
        os.makedirs(session_dir, exist_ok=True)

        # Data buffers (hold context window)
        buffer_size = sensor_sample_rate * self.CONTEXT_SECONDS * 2  # 2x for safety
        self.accel_buffer = deque(maxlen=buffer_size)
        self.gyro_buffer = deque(maxlen=buffer_size)
        self.gps_buffer = deque(maxlen=buffer_size)

        # Incident tracking
        self.incidents = []
        self.last_incident_time = None
        self.incident_cooldown = 5.0  # seconds (prevent duplicate logging)

        # Thread safety
        self.lock = threading.Lock()

    def add_accelerometer_sample(self, magnitude, timestamp=None):
        """Add accelerometer sample to buffer."""
        if timestamp is None:
            timestamp = time.time()

        with self.lock:
            self.accel_buffer.append({
                'timestamp': timestamp,
                'magnitude': magnitude
            })

    def add_gyroscope_sample(self, angular_velocity, timestamp=None):
        """Add gyroscope sample to buffer."""
        if timestamp is None:
            timestamp = time.time()

        with self.lock:
            self.gyro_buffer.append({
                'timestamp': timestamp,
                'angular_velocity': angular_velocity
            })

    def add_gps_sample(self, latitude, longitude, speed, accuracy, timestamp=None):
        """Add GPS sample to buffer."""
        if timestamp is None:
            timestamp = time.time()

        with self.lock:
            self.gps_buffer.append({
                'timestamp': timestamp,
                'latitude': latitude,
                'longitude': longitude,
                'speed': speed,
                'accuracy': accuracy
            })

    def check_hard_braking(self, magnitude):
        """Check if deceleration exceeds hard braking threshold."""
        if magnitude > self.THRESHOLDS['hard_braking']:
            self._log_incident('hard_braking', magnitude)

    def check_impact(self, magnitude):
        """Check if impact exceeds threshold."""
        if magnitude > self.THRESHOLDS['impact']:
            self._log_incident('impact', magnitude)

    def check_swerving(self, angular_velocity):
        """Check if swerving exceeds threshold."""
        if abs(angular_velocity) > self.THRESHOLDS['swerving']:
            self._log_incident('swerving', angular_velocity)

    def _log_incident(self, incident_type, magnitude):
        """
        Log incident with context data.

        Args:
            incident_type (str): 'hard_braking', 'impact', or 'swerving'
            magnitude (float): Value that triggered the incident
        """
        current_time = time.time()

        with self.lock:
            # Check cooldown (prevent duplicate logging)
            if self.last_incident_time and (current_time - self.last_incident_time) < self.incident_cooldown:
                return

            self.last_incident_time = current_time

            # Extract context windows
            accel_context = self._extract_context(self.accel_buffer, current_time)
            gyro_context = self._extract_context(self.gyro_buffer, current_time)
            gps_context = self._extract_context(self.gps_buffer, current_time)

            # Create incident record
            incident = {
                'event_type': incident_type,
                'magnitude': magnitude,
                'timestamp': current_time,
                'datetime': datetime.fromtimestamp(current_time).isoformat(),
                'context_seconds': self.CONTEXT_SECONDS,
                'accelerometer_samples': accel_context,
                'gyroscope_samples': gyro_context,
                'gps_samples': gps_context,
                'threshold': self.THRESHOLDS[incident_type]
            }

            self.incidents.append(incident)
            self._save_incident(incident)

    def _extract_context(self, buffer, event_time):
        """Extract data points within context window of event time."""
        context_window = self.CONTEXT_SECONDS

        return [
            item for item in buffer
            if abs(item['timestamp'] - event_time) <= context_window
        ]

    def _save_incident(self, incident):
        """
        Save incident to JSON file.

        Filename format: incident_TIMESTAMP_TYPE.json
        Example: incident_1729721945.234_impact.json
        """
        timestamp_str = str(incident['timestamp']).replace('.', '_')
        filename = f"incident_{timestamp_str}_{incident['event_type']}.json"
        filepath = os.path.join(self.session_dir, filename)

        try:
            with open(filepath, 'w') as f:
                json.dump(incident, f, indent=2)

            print(f"✓ Incident logged: {incident['event_type']} "
                  f"(magnitude: {incident['magnitude']:.2f}) → {filename}")

        except Exception as e:
            print(f"⚠ Failed to save incident: {e}")

    def get_incidents(self):
        """Get all logged incidents."""
        with self.lock:
            return list(self.incidents)

    def get_summary(self):
        """Get summary of incidents."""
        with self.lock:
            summary = {
                'total_incidents': len(self.incidents),
                'by_type': {},
                'latest_incident': None
            }

            for incident in self.incidents:
                event_type = incident['event_type']
                if event_type not in summary['by_type']:
                    summary['by_type'][event_type] = 0
                summary['by_type'][event_type] += 1

            if self.incidents:
                summary['latest_incident'] = {
                    'type': self.incidents[-1]['event_type'],
                    'magnitude': self.incidents[-1]['magnitude'],
                    'time': self.incidents[-1]['datetime']
                }

            return summary


# Example usage and testing
if __name__ == '__main__':
    print("Incident Detector Test\n" + "="*60)

    detector = IncidentDetector(session_dir='/tmp/test_incidents/')

    # Simulate some sensor data
    base_time = time.time()

    # Add some normal driving data
    for i in range(100):
        t = base_time + i * 0.02  # 50 Hz sampling
        detector.add_accelerometer_sample(magnitude=0.15, timestamp=t)
        detector.add_gyroscope_sample(angular_velocity=5.0, timestamp=t)
        detector.add_gps_sample(37.7749 + i*0.00001, -122.4194, 15.0, 5.0, timestamp=t)

    print("\n1. Normal driving data added (100 samples @ 50Hz)")

    # Simulate hard braking
    print("\n2. Simulating hard braking event...")
    brake_time = base_time + 100
    detector.add_accelerometer_sample(magnitude=0.85, timestamp=brake_time)
    detector.check_hard_braking(0.85)

    # Simulate impact
    print("\n3. Simulating impact event...")
    impact_time = base_time + 200
    detector.add_accelerometer_sample(magnitude=1.6, timestamp=impact_time)
    detector.check_impact(1.6)

    # Simulate swerving
    print("\n4. Simulating swerving event...")
    swerve_time = base_time + 300
    detector.add_gyroscope_sample(angular_velocity=1.2, timestamp=swerve_time)
    detector.check_swerving(1.2)

    # Get summary
    print("\n" + "="*60)
    print("Summary:")
    summary = detector.get_summary()
    print(f"  Total incidents: {summary['total_incidents']}")
    print(f"  By type: {summary['by_type']}")
    if summary['latest_incident']:
        print(f"  Latest: {summary['latest_incident']['type']} "
              f"({summary['latest_incident']['magnitude']:.2f}) "
              f"at {summary['latest_incident']['time']}")

    print("\n✓ Test complete - incidents saved to /tmp/test_incidents/")
