#!/usr/bin/env python3
"""
Motion Tracker Snapshot Test - Memory Diagnostic
Captures periodic snapshots of memory usage and data structure sizes
to identify where memory is being consumed.
"""

import subprocess
import json
import time
import math
import signal
import os
import sys
import threading
import psutil
import tracemalloc
from queue import Queue, Empty
from datetime import datetime
from collections import deque
from statistics import mean
from typing import Dict, List

# Import the benchmark classes
from motion_tracker_benchmark import (
    SensorFusion, GPSThread, AccelerometerThread,
    RateTracker, BenchmarkTracker
)

class MemorySnapshot:
    """Captures memory state at a point in time"""

    def __init__(self, timestamp, elapsed_seconds):
        self.timestamp = timestamp
        self.elapsed_seconds = elapsed_seconds
        self.memory_info = psutil.virtual_memory()
        self.memory_percent = self.memory_info.percent
        self.memory_used_mb = self.memory_info.used / (1024 * 1024)
        self.memory_available_mb = self.memory_info.available / (1024 * 1024)
        self.process_rss_mb = 0
        self.process_vms_mb = 0

        try:
            process = psutil.Process()
            self.process_rss_mb = process.memory_info().rss / (1024 * 1024)
            self.process_vms_mb = process.memory_info().vms / (1024 * 1024)
        except:
            pass

    def to_dict(self):
        return {
            'timestamp': self.timestamp.isoformat(),
            'elapsed_seconds': self.elapsed_seconds,
            'system_memory_percent': self.memory_percent,
            'system_memory_used_mb': round(self.memory_used_mb, 2),
            'system_memory_available_mb': round(self.memory_available_mb, 2),
            'process_rss_mb': round(self.process_rss_mb, 2),
            'process_vms_mb': round(self.process_vms_mb, 2)
        }

class DataStructureSnapshot:
    """Captures sizes of data structures in each tracker"""

    def __init__(self, timestamp, elapsed_seconds, trackers: Dict, queues: Dict):
        self.timestamp = timestamp
        self.elapsed_seconds = elapsed_seconds
        self.tracker_states = {}
        self.queue_states = {}

        # Snapshot each tracker's data structures
        for rate_name, tracker in trackers.items():
            self.tracker_states[rate_name] = {
                'gps_samples_count': len(tracker.gps_samples),
                'accel_samples_count': len(tracker.accel_samples),
                'velocity_errors_count': len(tracker.metrics['velocity_errors']),
                'gps_sample_count_metric': tracker.metrics['gps_sample_count'],
                'accel_sample_count_metric': tracker.metrics['accel_sample_count'],
            }

        # Snapshot queue sizes
        for rate_name, queue_list in queues.items():
            self.queue_states[rate_name] = [q.qsize() for q in queue_list]

    def to_dict(self):
        return {
            'timestamp': self.timestamp.isoformat(),
            'elapsed_seconds': self.elapsed_seconds,
            'trackers': self.tracker_states,
            'queues': self.queue_states
        }

class SnapshotBenchmarkTracker(BenchmarkTracker):
    """Extended benchmark tracker with snapshot diagnostics"""

    def __init__(self, rates=[10, 25, 50, 100], snapshot_interval=30):
        super().__init__(rates=rates)
        self.snapshot_interval = snapshot_interval
        self.memory_snapshots: List[MemorySnapshot] = []
        self.data_snapshots: List[DataStructureSnapshot] = []
        self.last_snapshot_time = None

    def take_snapshot(self):
        """Take a memory and data structure snapshot"""
        current_time = time.time()
        elapsed = (datetime.now() - self.start_time).total_seconds()

        # Memory snapshot
        mem_snap = MemorySnapshot(datetime.now(), elapsed)
        self.memory_snapshots.append(mem_snap)

        # Data structure snapshot
        queues_dict = {f"{rate}hz": [self.gps_queues[i], self.accel_queues[i]]
                       for i, rate in enumerate(self.rates)}
        data_snap = DataStructureSnapshot(datetime.now(), elapsed, self.trackers,
                                         {k: [self.gps_queues[self.rates.index(self.trackers[k].rate_hz)],
                                               self.accel_queues[self.rates.index(self.trackers[k].rate_hz)]]
                                          for k in self.trackers.keys()})
        self.data_snapshots.append(data_snap)

        return mem_snap, data_snap

    def track(self, duration_minutes=None):
        """Main tracking loop with snapshot diagnostics"""
        self.start_time = datetime.now()

        print("\n" + "="*80)
        print("MOTION TRACKER BENCHMARK - SNAPSHOT DIAGNOSTIC")
        print("="*80)
        print(f"Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Testing rates: {', '.join(f'{r} Hz' for r in self.rates)}")
        print(f"Snapshot interval: {self.snapshot_interval} seconds")

        if duration_minutes:
            print(f"Duration: {duration_minutes} minutes")
        else:
            print("Duration: Continuous (Ctrl+C to stop)")

        # Acquire wakelock
        try:
            subprocess.run(['termux-wake-lock'], check=False, capture_output=True)
            print("✓ Wakelock acquired")
        except:
            print("⚠ Could not acquire wakelock")

        # Start threads
        self.start_threads()

        print("\nWaiting for GPS fix (or timeout in 10s)...", flush=True)

        # Wait for first GPS fix
        gps_locked = False
        gps_wait_start = time.time()
        while not gps_locked and not self.shutdown_requested:
            try:
                gps_data = self.gps_queues[0].get(timeout=1)
                if gps_data and gps_data.get('latitude'):
                    print(f"✓ GPS locked: {gps_data['latitude']:.6f}, {gps_data['longitude']:.6f}\n")
                    gps_locked = True
            except Empty:
                if time.time() - gps_wait_start > 10:
                    print("⚠ GPS timeout - continuing without GPS lock (test mode)\n")
                    break

        if not gps_locked:
            print("⚠ No GPS lock available, running in simulation mode")

        print("Tracking with diagnostics... (Press Ctrl+C to stop)\n")
        print(f"Collecting snapshots every {self.snapshot_interval} seconds\n")

        last_save_time = time.time()
        last_display_time = time.time()
        self.last_snapshot_time = time.time()

        try:
            while not self.shutdown_requested:
                current_time = time.time()
                elapsed = (datetime.now() - self.start_time).total_seconds()

                # Check duration
                if duration_minutes and elapsed > duration_minutes * 60:
                    break

                # Take periodic snapshots
                if current_time - self.last_snapshot_time >= self.snapshot_interval:
                    mem_snap, data_snap = self.take_snapshot()
                    print(f"\n📸 SNAPSHOT @ {elapsed:.0f}s:")
                    print(f"   Memory: {mem_snap.memory_used_mb:.1f}MB used, "
                          f"{mem_snap.memory_available_mb:.1f}MB available "
                          f"({mem_snap.memory_percent:.1f}%)")
                    print(f"   Process: RSS={mem_snap.process_rss_mb:.1f}MB, "
                          f"VMS={mem_snap.process_vms_mb:.1f}MB")

                    # Show data structure sizes
                    for rate_name, state in data_snap.tracker_states.items():
                        print(f"   {rate_name}: GPS={state['gps_samples_count']}, "
                              f"Accel={state['accel_samples_count']}, "
                              f"Errors={state['velocity_errors_count']}")

                    self.last_snapshot_time = current_time

                    # Check if we're running low on memory
                    if mem_snap.memory_percent > 85:
                        print(f"\n   ⚠️ CRITICAL MEMORY: {mem_snap.memory_percent:.1f}% - saving snapshot and exiting")
                        break

                # Auto-save check
                if current_time - last_save_time >= self.snapshot_interval * 2:
                    print(f"\n⏰ Auto-saving...", flush=True)
                    self.save_data(auto_save=True)
                    last_save_time = current_time

                # Check memory before processing
                memory_throttle = self.check_memory()

                # Process all rates
                for i, (rate_name, tracker) in enumerate(self.trackers.items()):
                    # Process GPS queue
                    try:
                        while True:
                            gps_data = self.gps_queues[i].get_nowait()
                            if gps_data and gps_data.get('latitude'):
                                tracker.process_gps(gps_data, elapsed)
                    except Empty:
                        pass

                    # Process accelerometer queue
                    try:
                        batch_count = 0
                        queue_size = self.accel_queues[i].qsize()

                        if memory_throttle == 2:
                            max_batch = min(5, queue_size + 1)
                        elif memory_throttle == 1:
                            max_batch = min(10, queue_size + 1)
                        else:
                            max_batch = min(50, queue_size + 1)

                        while batch_count < max_batch:
                            accel_data = self.accel_queues[i].get_nowait()
                            if accel_data:
                                tracker.process_accel(accel_data, elapsed)
                                batch_count += 1
                    except Empty:
                        pass

                    # Detect queue overflow
                    if queue_size > self.accel_queues[i].maxsize * 0.9:
                        self.queue_overflows[rate_name] += 1

                # Display update
                if current_time - last_display_time >= 5.0:
                    print(f"\n--- Status @ {int(elapsed//60)}:{int(elapsed%60):02d} ---")
                    for rate_name, tracker in self.trackers.items():
                        queue_fill = (self.accel_queues[self.rates.index(tracker.rate_hz)].qsize() /
                                     self.accel_queues[self.rates.index(tracker.rate_hz)].maxsize) * 100
                        print(f"{rate_name:>6}: GPS={tracker.metrics['gps_sample_count']:>4}, "
                              f"Accel={tracker.metrics['accel_sample_count']:>6}, "
                              f"Queue={queue_fill:.0f}%")
                    last_display_time = current_time

                time.sleep(0.01)

        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        except Exception as e:
            print(f"\n\n⚠ ERROR: {e}")
            traceback.print_exc()

        # Stop threads
        print("\nStopping threads...")
        self.stop_event.set()

        if self.gps_thread:
            self.gps_thread.join(timeout=2)
        for thread in self.accel_threads:
            thread.join(timeout=2)

        print("✓ Threads stopped")

        # Release wakelock
        try:
            subprocess.run(['termux-wake-unlock'], check=False)
            print("✓ Wakelock released")
        except:
            pass

        # Save all snapshots
        print("\nGenerating snapshot diagnostic report...")
        self.print_summary()
        self.save_snapshots()
        self.save_data(auto_save=False)

    def save_snapshots(self):
        """Save all snapshots to diagnostic file"""
        timestamp = self.start_time.strftime('%Y%m%d_%H%M%S')

        snapshot_data = {
            'test_type': 'motion_tracker_snapshot_diagnostic',
            'start_time': self.start_time.isoformat(),
            'end_time': datetime.now().isoformat(),
            'snapshot_interval': self.snapshot_interval,
            'rates_tested': self.rates,
            'memory_snapshots': [s.to_dict() for s in self.memory_snapshots],
            'data_snapshots': [s.to_dict() for s in self.data_snapshots]
        }

        filename = f"snapshot_diagnostic_{timestamp}.json"
        with open(filename, 'w') as f:
            json.dump(snapshot_data, f, indent=2)

        print(f"\n✓ Snapshot diagnostic saved to: {filename}")

        # Print summary
        if self.memory_snapshots:
            first = self.memory_snapshots[0]
            last = self.memory_snapshots[-1]

            print(f"\nMemory Growth Summary:")
            print(f"  Start: {first.memory_used_mb:.1f}MB ({first.memory_percent:.1f}%)")
            print(f"  End:   {last.memory_used_mb:.1f}MB ({last.memory_percent:.1f}%)")
            print(f"  Growth: {last.memory_used_mb - first.memory_used_mb:.1f}MB")
            print(f"  Process RSS Growth: {last.process_rss_mb - first.process_rss_mb:.1f}MB")

def main():
    duration = None
    rates = [10, 25, 50, 100]
    snapshot_interval = 30

    if len(sys.argv) > 1:
        duration = int(sys.argv[1])

    if len(sys.argv) > 2:
        snapshot_interval = int(sys.argv[2])

    print(f"\nConfiguration:")
    print(f"  Rates: {', '.join(f'{r} Hz' for r in rates)}")
    print(f"  Snapshot interval: {snapshot_interval} seconds")
    if duration:
        print(f"  Duration: {duration} minutes")
    else:
        print(f"  Duration: Continuous (Ctrl+C to stop)")
    print("\nStarting in 3 seconds...")
    time.sleep(3)

    tracker = SnapshotBenchmarkTracker(rates=rates, snapshot_interval=snapshot_interval)
    tracker.track(duration_minutes=duration)

if __name__ == "__main__":
    main()
