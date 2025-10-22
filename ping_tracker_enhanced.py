#!/usr/bin/env python3
"""
Enhanced Internet Ping Tracker with Spike Detection & Notifications
Monitors internet connectivity with high resolution and alerts on anomalies
"""

import subprocess
import time
import json
import sys
import os
from datetime import datetime, timedelta
from statistics import mean, median, stdev

class EnhancedPingTracker:
    def __init__(self, host='8.8.8.8', interval=30, duration_hours=2, spike_threshold=100):
        self.host = host
        self.interval = interval  # seconds between pings
        self.duration = duration_hours * 3600  # convert to seconds
        self.spike_threshold = spike_threshold  # ms - alert if ping exceeds this
        self.results = []
        self.start_time = None
        self.end_time = None
        self.spike_count = 0
        self.consecutive_spikes = 0

        # Acquire wakelock automatically
        self.acquire_wakelock()

    def acquire_wakelock(self):
        """Automatically acquire Termux wakelock to prevent device sleep"""
        try:
            subprocess.run(['termux-wake-lock'], check=False)
            print("✓ Wakelock acquired - device will stay awake")
        except Exception as e:
            print(f"⚠ Warning: Could not acquire wakelock: {e}")

    def release_wakelock(self):
        """Release the wakelock when done"""
        try:
            subprocess.run(['termux-wake-unlock'], check=False)
            print("✓ Wakelock released")
        except Exception as e:
            print(f"⚠ Warning: Could not release wakelock: {e}")

    def send_notification(self, title, message):
        """Send Termux notification"""
        try:
            subprocess.run(
                ['termux-notification', '--title', title, '--content', message],
                check=False
            )
        except Exception:
            pass  # Silently fail if notifications not available

    def ping_once(self):
        """Execute a single ping and return the result"""
        try:
            result = subprocess.run(
                ['ping', '-c', '1', '-W', '2', self.host],
                capture_output=True,
                text=True,
                timeout=3
            )

            timestamp = datetime.now()

            if result.returncode == 0:
                # Parse ping time from output
                output = result.stdout
                for line in output.split('\n'):
                    if 'time=' in line:
                        time_str = line.split('time=')[1].split()[0]
                        ping_ms = float(time_str)
                        return {
                            'timestamp': timestamp.isoformat(),
                            'status': 'success',
                            'ping_ms': ping_ms
                        }

            # Ping failed
            return {
                'timestamp': timestamp.isoformat(),
                'status': 'failed',
                'ping_ms': None
            }

        except Exception as e:
            return {
                'timestamp': datetime.now().isoformat(),
                'status': 'error',
                'ping_ms': None,
                'error': str(e)
            }

    def check_for_anomalies(self, result, ping_count):
        """Detect and alert on anomalies"""
        if result['status'] == 'success' and result['ping_ms']:
            ping_ms = result['ping_ms']

            # Check for spike
            if ping_ms > self.spike_threshold:
                self.spike_count += 1
                self.consecutive_spikes += 1

                # Alert on spike
                alert_msg = f"⚠ SPIKE #{self.spike_count}: {ping_ms:.1f}ms (threshold: {self.spike_threshold}ms)"
                print(f"\n{alert_msg}")

                # Send notification
                self.send_notification(
                    "Ping Spike Detected",
                    f"{ping_ms:.1f}ms on ping #{ping_count}"
                )

                # Extra alert for consecutive spikes
                if self.consecutive_spikes >= 2:
                    cluster_msg = f"🔴 CLUSTER ALERT: {self.consecutive_spikes} consecutive spikes!"
                    print(cluster_msg)
                    self.send_notification(
                        "Spike Cluster Detected",
                        f"{self.consecutive_spikes} consecutive high-latency pings"
                    )
            else:
                self.consecutive_spikes = 0

        elif result['status'] == 'failed':
            # Alert on failure
            print(f"\n🔴 PING FAILED on attempt #{ping_count}")
            self.send_notification(
                "Ping Failed",
                f"Connection failure on ping #{ping_count}"
            )
            self.consecutive_spikes = 0

    def calculate_running_stats(self):
        """Calculate statistics from recent pings"""
        if len(self.results) < 1:
            return None

        # Get last 10 successful pings for running average
        recent_pings = [r['ping_ms'] for r in self.results[-10:]
                       if r['status'] == 'success' and r['ping_ms']]

        if not recent_pings:
            return None

        return {
            'avg': mean(recent_pings),
            'min': min(recent_pings),
            'max': max(recent_pings)
        }

    def run(self):
        """Run the ping tracker for the specified duration"""
        self.start_time = datetime.now()
        end_time = self.start_time + timedelta(seconds=self.duration)

        print(f"\n{'='*70}")
        print("ENHANCED PING TRACKER - High Resolution Mode")
        print(f"{'='*70}")
        print(f"Host: {self.host}")
        print(f"Interval: {self.interval} seconds (HIGH RESOLUTION)")
        print(f"Duration: {self.duration/3600:.1f} hours")
        print(f"Spike Threshold: {self.spike_threshold}ms")
        print(f"Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"End time: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Expected pings: ~{int(self.duration/self.interval)}")
        print(f"\n🔔 Notifications enabled for spikes and failures")
        print(f"\nTracking... (Press Ctrl+C to stop early)\n")

        ping_count = 0

        try:
            while datetime.now() < end_time:
                result = self.ping_once()
                self.results.append(result)
                ping_count += 1

                # Check for anomalies
                self.check_for_anomalies(result, ping_count)

                # Display progress
                status_symbol = '✓' if result['status'] == 'success' else '✗'
                ping_display = f"{result['ping_ms']:.1f}ms" if result['ping_ms'] else "FAILED"
                elapsed = (datetime.now() - self.start_time).total_seconds() / 3600

                # Add spike indicator
                spike_indicator = "⚠" if (result['status'] == 'success' and
                                         result['ping_ms'] and
                                         result['ping_ms'] > self.spike_threshold) else " "

                # Calculate running stats
                stats = self.calculate_running_stats()
                stats_display = ""
                if stats:
                    stats_display = f"| Avg(10): {stats['avg']:>5.1f}ms"

                print(f"[{ping_count:4d}] {status_symbol} {ping_display:>10} {spike_indicator} | "
                      f"Elapsed: {elapsed:.2f}h {stats_display}")

                # Wait for next interval
                time.sleep(self.interval)

        except KeyboardInterrupt:
            print("\n\n⚠ Stopped by user.")

        self.end_time = datetime.now()
        self.release_wakelock()
        self.save_results()
        self.print_report()

    def save_results(self):
        """Save results to JSON file"""
        filename = f"ping_enhanced_{self.start_time.strftime('%Y%m%d_%H%M%S')}.json"

        data = {
            'host': self.host,
            'interval': self.interval,
            'spike_threshold': self.spike_threshold,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat(),
            'spike_count': self.spike_count,
            'results': self.results
        }

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"\n✓ Results saved to: {filename}")

    def print_report(self):
        """Generate and print analysis report"""
        if not self.results:
            print("No results to analyze.")
            return

        total_pings = len(self.results)
        successful_pings = [r for r in self.results if r['status'] == 'success']
        failed_pings = [r for r in self.results if r['status'] == 'failed']

        success_count = len(successful_pings)
        failure_count = len(failed_pings)
        success_rate = (success_count / total_pings * 100) if total_pings > 0 else 0

        ping_times = [r['ping_ms'] for r in successful_pings]

        # Calculate duration
        duration = (self.end_time - self.start_time).total_seconds()

        print("\n" + "="*70)
        print("ENHANCED PING TRACKER REPORT")
        print("="*70)
        print(f"\nHost: {self.host}")
        print(f"Duration: {duration/3600:.2f} hours ({duration/60:.1f} minutes)")
        print(f"Start: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"End: {self.end_time.strftime('%Y-%m-%d %H:%M:%S')}")

        print(f"\n{'CONNECTION STATISTICS':-^70}")
        print(f"Total pings: {total_pings}")
        print(f"Successful: {success_count} ({success_rate:.1f}%)")
        print(f"Failed: {failure_count} ({100-success_rate:.1f}%)")
        print(f"Spikes (>{self.spike_threshold}ms): {self.spike_count}")

        if ping_times:
            print(f"\n{'LATENCY STATISTICS':-^70}")
            print(f"Average ping: {mean(ping_times):.2f} ms")
            print(f"Median ping: {median(ping_times):.2f} ms")
            print(f"Min ping: {min(ping_times):.2f} ms")
            print(f"Max ping: {max(ping_times):.2f} ms")

            if len(ping_times) > 1:
                print(f"Std deviation: {stdev(ping_times):.2f} ms")
                jitter = stdev(ping_times)
                print(f"Network jitter: {jitter:.2f} ms {'(STABLE)' if jitter < 20 else '(UNSTABLE)' if jitter < 50 else '(VERY UNSTABLE)'}")

            # Categorize latency
            excellent = sum(1 for p in ping_times if p < 20)
            good = sum(1 for p in ping_times if 20 <= p < 50)
            fair = sum(1 for p in ping_times if 50 <= p < 100)
            poor = sum(1 for p in ping_times if p >= 100)

            print(f"\n{'LATENCY BREAKDOWN':-^70}")
            print(f"Excellent (<20ms): {excellent:4d} ({excellent/len(ping_times)*100:5.1f}%)")
            print(f"Good (20-50ms):    {good:4d} ({good/len(ping_times)*100:5.1f}%)")
            print(f"Fair (50-100ms):   {fair:4d} ({fair/len(ping_times)*100:5.1f}%)")
            print(f"Poor (>100ms):     {poor:4d} ({poor/len(ping_times)*100:5.1f}%)")

        # Detect spike clusters
        spike_clusters = []
        cluster_start = None

        for i, result in enumerate(self.results):
            if result['status'] == 'success' and result['ping_ms'] and result['ping_ms'] > self.spike_threshold:
                if cluster_start is None:
                    cluster_start = i
            else:
                if cluster_start is not None and (i - cluster_start) >= 2:
                    spike_clusters.append((cluster_start, i - 1))
                cluster_start = None

        if cluster_start is not None and (len(self.results) - cluster_start) >= 2:
            spike_clusters.append((cluster_start, len(self.results) - 1))

        if spike_clusters:
            print(f"\n{'SPIKE CLUSTERS DETECTED':-^70}")
            for start_idx, end_idx in spike_clusters:
                duration_min = (end_idx - start_idx + 1) * self.interval / 60
                start_time = datetime.fromisoformat(self.results[start_idx]['timestamp'])
                avg_spike = mean([self.results[i]['ping_ms'] for i in range(start_idx, end_idx + 1)])
                print(f"• {start_time.strftime('%H:%M:%S')} - {duration_min:.1f} min "
                      f"({end_idx - start_idx + 1} spikes, avg: {avg_spike:.1f}ms)")

        # Find outages
        outages = []
        current_outage_start = None

        for i, result in enumerate(self.results):
            if result['status'] == 'failed':
                if current_outage_start is None:
                    current_outage_start = i
            else:
                if current_outage_start is not None:
                    outages.append((current_outage_start, i - 1))
                    current_outage_start = None

        if current_outage_start is not None:
            outages.append((current_outage_start, len(self.results) - 1))

        if outages:
            print(f"\n{'OUTAGES DETECTED':-^70}")
            for start_idx, end_idx in outages:
                duration_min = (end_idx - start_idx + 1) * self.interval / 60
                start_time = datetime.fromisoformat(self.results[start_idx]['timestamp'])
                print(f"• {start_time.strftime('%H:%M:%S')} - {duration_min:.1f} min "
                      f"({end_idx - start_idx + 1} failed pings)")

        print("\n" + "="*70)


def main():
    print("\n" + "="*70)
    print("ENHANCED INTERNET PING TRACKER")
    print("High Resolution Monitoring with Spike Detection")
    print("="*70 + "\n")

    # Get user input
    try:
        host = input("Host to ping [default: 8.8.8.8]: ").strip() or "8.8.8.8"

        interval_input = input("Ping interval in seconds [default: 30]: ").strip()
        interval = int(interval_input) if interval_input else 30

        duration_input = input("Duration in hours [default: 2]: ").strip()
        duration = float(duration_input) if duration_input else 2

        threshold_input = input("Spike threshold in ms [default: 100]: ").strip()
        threshold = int(threshold_input) if threshold_input else 100

        tracker = EnhancedPingTracker(
            host=host,
            interval=interval,
            duration_hours=duration,
            spike_threshold=threshold
        )
        tracker.run()

    except KeyboardInterrupt:
        print("\n\nExiting...")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
