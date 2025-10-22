#!/usr/bin/env python3
"""
GPS Diagnostic Tool
Monitors GPS behavior while stationary to characterize noise, drift, and accuracy
"""

import subprocess
import json
import time
import math
from datetime import datetime
from statistics import mean, stdev

class GPSTester:
    """GPS diagnostic and testing tool"""

    def __init__(self):
        self.baseline_position = None
        self.samples = []
        self.start_time = None

    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two GPS coordinates in meters"""
        R = 6371000  # Earth radius in meters

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)

        a = (math.sin(delta_phi/2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        return R * c

    def read_gps(self, timeout=15):
        """Read GPS data from Termux API"""
        try:
            result = subprocess.run(
                ['termux-location', '-p', 'gps'],
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                return {
                    'latitude': data.get('latitude'),
                    'longitude': data.get('longitude'),
                    'altitude': data.get('altitude'),
                    'speed': data.get('speed'),
                    'bearing': data.get('bearing'),
                    'accuracy': data.get('accuracy'),
                    'timestamp': datetime.now().isoformat()
                }
        except Exception as e:
            print(f"⚠ GPS Error: {e}")

        return None

    def calculate_statistics(self):
        """Calculate drift and noise statistics"""
        if len(self.samples) < 2:
            return None

        drifts = [s['drift'] for s in self.samples if s.get('drift') is not None]
        speeds = [s['speed'] for s in self.samples if s.get('speed') is not None]
        accuracies = [s['accuracy'] for s in self.samples if s.get('accuracy') is not None]

        stats = {}

        if drifts:
            stats['drift'] = {
                'mean': mean(drifts),
                'max': max(drifts),
                'min': min(drifts),
                'stdev': stdev(drifts) if len(drifts) > 1 else 0
            }

        if speeds:
            stats['speed'] = {
                'mean': mean(speeds),
                'max': max(speeds),
                'min': min(speeds),
                'stdev': stdev(speeds) if len(speeds) > 1 else 0
            }

        if accuracies:
            stats['accuracy'] = {
                'mean': mean(accuracies),
                'max': max(accuracies),
                'min': min(accuracies),
                'stdev': stdev(accuracies) if len(accuracies) > 1 else 0
            }

        return stats

    def run(self, duration_minutes=5, update_interval=2):
        """Run GPS testing for specified duration"""
        self.start_time = datetime.now()

        print("\n" + "="*80)
        print("GPS DIAGNOSTIC TOOL - Stationary Testing")
        print("="*80)
        print(f"Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Duration: {duration_minutes} minutes")
        print(f"Update interval: {update_interval} seconds")
        print("\nAcquiring GPS lock...\n")

        # Get baseline position
        while self.baseline_position is None:
            gps_data = self.read_gps()
            if gps_data and gps_data['latitude']:
                self.baseline_position = {
                    'lat': gps_data['latitude'],
                    'lon': gps_data['longitude'],
                    'alt': gps_data['altitude']
                }
                print(f"✓ Baseline position locked:")
                print(f"  Lat: {self.baseline_position['lat']:.8f}")
                print(f"  Lon: {self.baseline_position['lon']:.8f}")
                print(f"  Alt: {self.baseline_position['alt']:.1f}m")
                print(f"  Accuracy: {gps_data['accuracy']:.1f}m")
                print("\n" + "="*80)
                print("Keep device STATIONARY - monitoring GPS drift/noise...")
                print("="*80)
                print(f"\n{'Time':<8} | {'Drift(m)':<10} | {'Speed':<10} | {'Accuracy':<10} | {'Status':<15}")
                print("-" * 80)
                break
            time.sleep(1)

        # Monitor GPS over time
        try:
            end_time = time.time() + (duration_minutes * 60)

            while time.time() < end_time:
                elapsed = (datetime.now() - self.start_time).total_seconds()
                time_str = f"{int(elapsed//60)}:{int(elapsed%60):02d}"

                gps_data = self.read_gps(timeout=update_interval + 5)

                if gps_data and gps_data['latitude']:
                    # Calculate drift from baseline
                    drift = self.haversine_distance(
                        self.baseline_position['lat'],
                        self.baseline_position['lon'],
                        gps_data['latitude'],
                        gps_data['longitude']
                    )

                    # Store sample
                    sample = {
                        'elapsed': elapsed,
                        'drift': drift,
                        'speed': gps_data['speed'] if gps_data['speed'] else 0,
                        'accuracy': gps_data['accuracy'],
                        'gps': gps_data
                    }
                    self.samples.append(sample)

                    # Determine status based on drift vs accuracy
                    if drift < gps_data['accuracy']:
                        status = "✓ Within accuracy"
                    elif drift < gps_data['accuracy'] * 1.5:
                        status = "~ Near boundary"
                    else:
                        status = "⚠ Drifting"

                    # Display
                    print(f"{time_str:<8} | {drift:>8.2f}m | {sample['speed']:>8.2f} | {gps_data['accuracy']:>8.1f}m | {status}")

                else:
                    print(f"{time_str:<8} | {'N/A':<10} | {'N/A':<10} | {'N/A':<10} | ⚠ GPS unavailable")

                time.sleep(update_interval)

        except KeyboardInterrupt:
            print("\n\nStopped by user.")

        # Show statistics
        self.print_summary()
        self.save_data()

    def print_summary(self):
        """Print summary statistics"""
        if not self.samples:
            print("\nNo data collected")
            return

        print("\n" + "="*80)
        print("GPS DIAGNOSTIC SUMMARY")
        print("="*80)

        stats = self.calculate_statistics()

        if stats:
            print(f"\nSamples collected: {len(self.samples)}")
            print(f"Duration: {int(self.samples[-1]['elapsed']//60)}m {int(self.samples[-1]['elapsed']%60)}s")

            if 'drift' in stats:
                print(f"\nPosition Drift (meters):")
                print(f"  Mean:   {stats['drift']['mean']:.2f}m")
                print(f"  Max:    {stats['drift']['max']:.2f}m")
                print(f"  Min:    {stats['drift']['min']:.2f}m")
                print(f"  StdDev: {stats['drift']['stdev']:.2f}m")

            if 'speed' in stats:
                print(f"\nSpeed Readings (m/s) [should be ~0 when stationary]:")
                print(f"  Mean:   {stats['speed']['mean']:.2f} m/s ({stats['speed']['mean']*3.6:.2f} km/h)")
                print(f"  Max:    {stats['speed']['max']:.2f} m/s ({stats['speed']['max']*3.6:.2f} km/h)")
                print(f"  Min:    {stats['speed']['min']:.2f} m/s")
                print(f"  StdDev: {stats['speed']['stdev']:.2f} m/s")

            if 'accuracy' in stats:
                print(f"\nGPS Accuracy (meters):")
                print(f"  Mean:   {stats['accuracy']['mean']:.2f}m")
                print(f"  Best:   {stats['accuracy']['min']:.2f}m")
                print(f"  Worst:  {stats['accuracy']['max']:.2f}m")
                print(f"  StdDev: {stats['accuracy']['stdev']:.2f}m")

            # Recommendations
            print(f"\nRECOMMENDATIONS for Motion Tracker:")
            if 'drift' in stats and 'accuracy' in stats:
                suggested_threshold = max(stats['accuracy']['mean'] * 1.5, stats['drift']['max'] + 1)
                print(f"  Suggested stationary threshold: {suggested_threshold:.1f}m")
                print(f"  (Current: 5.0m or 1.5x accuracy, whichever is larger)")

            if 'speed' in stats:
                suggested_speed_threshold = stats['speed']['mean'] + (2 * stats['speed']['stdev'])
                print(f"  Suggested speed threshold: {suggested_speed_threshold:.2f} m/s ({suggested_speed_threshold*3.6:.2f} km/h)")
                print(f"  (Current: 0.5 m/s or 1.8 km/h)")

        print("="*80)

    def save_data(self):
        """Save diagnostic data to file"""
        filename = f"gps_diagnostic_{self.start_time.strftime('%Y%m%d_%H%M%S')}.json"

        stats = self.calculate_statistics()

        data = {
            'start_time': self.start_time.isoformat(),
            'end_time': datetime.now().isoformat(),
            'baseline_position': self.baseline_position,
            'statistics': stats,
            'samples': self.samples
        }

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"\n✓ Diagnostic data saved to: {filename}")
        print(f"  Use this to analyze GPS behavior and tune motion tracker thresholds")


def main():
    import sys

    print("\n" + "="*80)
    print("GPS DIAGNOSTIC TOOL")
    print("Characterize GPS noise and drift while stationary")
    print("="*80)

    print("\nInstructions:")
    print("  1. Place device in a stable location (don't move it!)")
    print("  2. Keep it stationary for the entire test")
    print("  3. The tool will monitor GPS drift, speed noise, and accuracy")
    print("  4. Results will help tune the motion tracker thresholds\n")

    try:
        # Parse command line arguments or use defaults
        duration = 5
        interval = 2

        if len(sys.argv) > 1:
            duration = int(sys.argv[1])
        if len(sys.argv) > 2:
            interval = int(sys.argv[2])

        print(f"Configuration:")
        print(f"  Duration: {duration} minutes")
        print(f"  Update interval: {interval} seconds")
        print("\nStarting test in 3 seconds...")
        print("Make sure device is STATIONARY!")
        time.sleep(3)

        tester = GPSTester()
        tester.run(duration_minutes=duration, update_interval=interval)

    except KeyboardInterrupt:
        print("\n\nExiting...")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
