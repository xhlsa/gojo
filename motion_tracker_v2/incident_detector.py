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

import copy
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
        # MEMORY OPTIMIZATION: Incidents saved to disk, only keep recent ones in memory
        # Each incident ~200 KB (30s context @ 44 Hz), so 20 * 200 KB = ~4 MB
        self.incidents = deque(maxlen=20)  # Keep last 20 for get_incidents() (was 100, saves ~16 MB)
        self.last_incident_time = None
        self.incident_cooldown = 5.0  # seconds (prevent duplicate logging)

        # Persistent counters
        self.total_incidents = 0
        self.incidents_by_type = {}
        self.counters_path = os.path.join(self.session_dir, 'session_counters.json')
        self._load_persistent_counters()

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

            # Update counters before deque truncation
            self.total_incidents += 1
            self.incidents_by_type[incident_type] = self.incidents_by_type.get(incident_type, 0) + 1

            self.incidents.append(incident)
            self._save_incident(incident)
            self._persist_counters()

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
                'total_incidents': self.total_incidents,
                'by_type': dict(self.incidents_by_type),
                'latest_incident': None
            }

            if self.incidents:
                summary['latest_incident'] = copy.deepcopy(self.incidents[-1])

            return summary

    def _load_persistent_counters(self):
        """Load counters from disk or rebuild from existing incidents."""
        if os.path.exists(self.counters_path):
            try:
                with open(self.counters_path, 'r') as f:
                    data = json.load(f)
                self.total_incidents = int(data.get('total_incidents', 0))
                stored_by_type = data.get('incidents_by_type', {})
                if isinstance(stored_by_type, dict):
                    self.incidents_by_type = {
                        key: int(value) for key, value in stored_by_type.items()
                    }
                else:
                    self.incidents_by_type = {}
                return
            except Exception as exc:
                print(f"⚠ Failed to load incident counters: {exc}. Rebuilding...")

        self._rebuild_counters_from_files()

    def _rebuild_counters_from_files(self):
        """Recalculate counters by scanning existing incident files."""
        total = 0
        by_type = {}

        for root, _, files in os.walk(self.session_dir):
            for filename in files:
                if not filename.startswith('incident_') or not filename.endswith('.json'):
                    continue
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, 'r') as f:
                        incident = json.load(f)
                except Exception:
                    continue

                event_type = incident.get('event_type')
                if not event_type:
                    continue

                total += 1
                by_type[event_type] = by_type.get(event_type, 0) + 1

        self.total_incidents = total
        self.incidents_by_type = by_type
        self._persist_counters()

    def _persist_counters(self):
        """Persist counters to disk so they survive restarts."""
        data = {
            'total_incidents': self.total_incidents,
            'incidents_by_type': self.incidents_by_type,
            'updated_at': datetime.utcnow().isoformat() + 'Z'
        }

        tmp_path = f"{self.counters_path}.tmp"
        try:
            with open(tmp_path, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.counters_path)
        except Exception as exc:
            print(f"⚠ Failed to persist incident counters: {exc}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


# Example usage and testing
if __name__ == '__main__':
    print("Incident Detector Test\n" + "="*60)

    # Use a unique persistent logs directory for repeatable testing
    base_test_dir = os.path.expanduser('~/gojo/motion_tracker_sessions')
    os.makedirs(base_test_dir, exist_ok=True)
    unique_suffix = int(time.time())
    test_incidents_dir = os.path.join(base_test_dir, f'test_incidents_{unique_suffix}')
    detector = IncidentDetector(session_dir=test_incidents_dir)

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
        latest = summary['latest_incident']
        print(f"  Latest: {latest['event_type']} "
              f"({latest['magnitude']:.2f}) at {latest['datetime']}")

    # Stress test: ensure counters stay accurate beyond 20 incidents
    detector.incident_cooldown = 0.0
    stress_events = 25
    pre_stress_total = summary['total_incidents']
    stress_start_time = base_time + 400
    print("\n5. Stress testing counters with >20 additional swerving events...")
    for i in range(stress_events):
        timestamp = stress_start_time + i * 0.1
        detector.add_gyroscope_sample(angular_velocity=1.3, timestamp=timestamp)
        detector.check_swerving(1.3)

    post_stress_summary = detector.get_summary()
    expected_total = pre_stress_total + stress_events
    print(f"  Expected total incidents: {expected_total}")
    print(f"  Reported total incidents: {post_stress_summary['total_incidents']}")
    if post_stress_summary['total_incidents'] != expected_total:
        raise SystemExit("✗ Counter mismatch detected!")
    print("  ✓ Counters remain accurate even when deque truncates old incidents.")

    print(f"\n✓ Test complete - incidents saved to {test_incidents_dir}")
