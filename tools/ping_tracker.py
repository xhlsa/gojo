#!/usr/bin/env python3
"""
Internet Ping Tracker
Monitors internet connectivity by pinging a server at regular intervals
and generates a detailed report.
"""

import subprocess
import time
import json
import sys
from datetime import datetime, timedelta
from statistics import mean, median, stdev

class PingTracker:
    def __init__(self, host='8.8.8.8', interval=60, duration_hours=2):
        self.host = host
        self.interval = interval  # seconds between pings
        self.duration = duration_hours * 3600  # convert to seconds
        self.results = []
        self.start_time = None
        self.end_time = None

    def ping_once(self):
        """Execute a single ping and return the result"""
        try:
            # Run ping command (works in Termux)
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
                # Look for time= in output
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

    def run(self):
        """Run the ping tracker for the specified duration"""
        self.start_time = datetime.now()
        end_time = self.start_time + timedelta(seconds=self.duration)

        print(f"Starting ping tracker...")
        print(f"Host: {self.host}")
        print(f"Interval: {self.interval} seconds")
        print(f"Duration: {self.duration/3600:.1f} hours")
        print(f"Start time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"End time: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"\nTracking... (Press Ctrl+C to stop early)\n")

        ping_count = 0

        try:
            while datetime.now() < end_time:
                result = self.ping_once()
                self.results.append(result)
                ping_count += 1

                # Display progress
                status_symbol = '✓' if result['status'] == 'success' else '✗'
                ping_display = f"{result['ping_ms']:.1f}ms" if result['ping_ms'] else "FAILED"
                elapsed = (datetime.now() - self.start_time).total_seconds() / 3600

                print(f"[{ping_count:4d}] {status_symbol} {ping_display:>10} | Elapsed: {elapsed:.2f}h")

                # Wait for next interval
                time.sleep(self.interval)

        except KeyboardInterrupt:
            print("\n\nStopped by user.")

        self.end_time = datetime.now()
        self.save_results()
        self.print_report()

    def save_results(self):
        """Save results to JSON file"""
        filename = f"ping_results_{self.start_time.strftime('%Y%m%d_%H%M%S')}.json"

        data = {
            'host': self.host,
            'interval': self.interval,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat(),
            'results': self.results
        }

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"\nResults saved to: {filename}")

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

        print("\n" + "="*60)
        print("PING TRACKER REPORT")
        print("="*60)
        print(f"\nHost: {self.host}")
        print(f"Duration: {duration/3600:.2f} hours ({duration/60:.1f} minutes)")
        print(f"Start: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"End: {self.end_time.strftime('%Y-%m-%d %H:%M:%S')}")

        print(f"\n{'CONNECTION STATISTICS':-^60}")
        print(f"Total pings: {total_pings}")
        print(f"Successful: {success_count} ({success_rate:.1f}%)")
        print(f"Failed: {failure_count} ({100-success_rate:.1f}%)")

        if ping_times:
            print(f"\n{'LATENCY STATISTICS':-^60}")
            print(f"Average ping: {mean(ping_times):.2f} ms")
            print(f"Median ping: {median(ping_times):.2f} ms")
            print(f"Min ping: {min(ping_times):.2f} ms")
            print(f"Max ping: {max(ping_times):.2f} ms")

            if len(ping_times) > 1:
                print(f"Std deviation: {stdev(ping_times):.2f} ms")

            # Categorize latency
            excellent = sum(1 for p in ping_times if p < 20)
            good = sum(1 for p in ping_times if 20 <= p < 50)
            fair = sum(1 for p in ping_times if 50 <= p < 100)
            poor = sum(1 for p in ping_times if p >= 100)

            print(f"\n{'LATENCY BREAKDOWN':-^60}")
            print(f"Excellent (<20ms): {excellent} ({excellent/len(ping_times)*100:.1f}%)")
            print(f"Good (20-50ms): {good} ({good/len(ping_times)*100:.1f}%)")
            print(f"Fair (50-100ms): {fair} ({fair/len(ping_times)*100:.1f}%)")
            print(f"Poor (>100ms): {poor} ({poor/len(ping_times)*100:.1f}%)")

        # Find outages (consecutive failures)
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
            print(f"\n{'OUTAGES DETECTED':-^60}")
            for start_idx, end_idx in outages:
                duration = (end_idx - start_idx + 1) * self.interval / 60
                start_time = datetime.fromisoformat(self.results[start_idx]['timestamp'])
                print(f"• {start_time.strftime('%H:%M:%S')} - {duration:.1f} minutes ({end_idx - start_idx + 1} failed pings)")

        print("\n" + "="*60)


def main():
    print("Internet Ping Tracker\n")

    # Get user input
    try:
        host = input("Host to ping [default: 8.8.8.8]: ").strip() or "8.8.8.8"

        interval_input = input("Ping interval in seconds [default: 60]: ").strip()
        interval = int(interval_input) if interval_input else 60

        duration_input = input("Duration in hours [default: 2]: ").strip()
        duration = float(duration_input) if duration_input else 2

        print()

        tracker = PingTracker(host=host, interval=interval, duration_hours=duration)
        tracker.run()

    except KeyboardInterrupt:
        print("\n\nExiting...")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
