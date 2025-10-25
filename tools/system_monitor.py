#!/usr/bin/env python3
"""
Comprehensive System Monitor with Correlation Analysis
Monitors network, CPU, memory, battery, WiFi, and processes simultaneously
"""

import subprocess
import time
import json
import sys
import os
import re
from datetime import datetime, timedelta
from statistics import mean, median, stdev
from collections import defaultdict

class SystemProbe:
    """Base class for system probes"""

    @staticmethod
    def safe_run(cmd):
        """Safely run a command and return output"""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=2,
                shell=True if isinstance(cmd, str) else False
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None


class NetworkProbe(SystemProbe):
    """Probe for network metrics"""

    def __init__(self, host='8.8.8.8'):
        self.host = host

    def probe(self):
        """Collect network metrics"""
        data = {}

        # Ping
        result = self.safe_run(['ping', '-c', '1', '-W', '2', self.host])
        if result:
            for line in result.split('\n'):
                if 'time=' in line:
                    time_str = line.split('time=')[1].split()[0]
                    data['ping_ms'] = float(time_str)
                    break

        if 'ping_ms' not in data:
            data['ping_ms'] = None
            data['ping_failed'] = True

        # WiFi signal strength (requires termux-api)
        wifi_info = self.safe_run('termux-wifi-connectioninfo 2>/dev/null')
        if wifi_info:
            try:
                wifi_data = json.loads(wifi_info)
                data['wifi_rssi'] = wifi_data.get('rssi')  # Signal strength in dBm
                data['wifi_link_speed'] = wifi_data.get('link_speed_mbps')
                data['wifi_ssid'] = wifi_data.get('ssid', '').replace('"', '')
            except:
                pass

        # Network stats from /proc
        net_stats = self.safe_run('cat /proc/net/dev')
        if net_stats:
            for line in net_stats.split('\n'):
                if 'wlan0' in line:
                    parts = line.split()
                    if len(parts) >= 10:
                        data['rx_bytes'] = int(parts[1])
                        data['tx_bytes'] = int(parts[9])

        return data


class ResourceProbe(SystemProbe):
    """Probe for CPU and memory metrics"""

    def probe(self):
        """Collect resource metrics"""
        data = {}

        # CPU usage - Use ps instead of top (top is broken in Termux)
        # Get sum of CPU usage from all processes
        ps_info = self.safe_run('ps aux 2>/dev/null')
        if ps_info:
            try:
                total_cpu = 0.0
                lines = ps_info.split('\n')

                # Skip header line
                for line in lines[1:]:
                    if line.strip():
                        parts = line.split()
                        if len(parts) >= 3:
                            try:
                                # %CPU is typically the 3rd column
                                cpu_val = float(parts[2])
                                total_cpu += cpu_val
                            except (ValueError, IndexError):
                                continue

                # Round to 1 decimal place
                data['cpu_percent'] = round(total_cpu, 1)
            except Exception:
                pass

        # Memory info
        mem_info = self.safe_run('cat /proc/meminfo')
        if mem_info:
            mem_data = {}
            for line in mem_info.split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    # Extract number (in kB)
                    num = re.search(r'(\d+)', value)
                    if num:
                        mem_data[key.strip()] = int(num.group(1))

            if 'MemTotal' in mem_data and 'MemAvailable' in mem_data:
                total = mem_data['MemTotal']
                available = mem_data['MemAvailable']
                used = total - available
                data['mem_total_mb'] = total // 1024
                data['mem_used_mb'] = used // 1024
                data['mem_available_mb'] = available // 1024
                data['mem_percent'] = (used / total * 100) if total > 0 else 0

        # CPU frequency (if available)
        cpu_freq = self.safe_run('cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null')
        if cpu_freq and cpu_freq.isdigit():
            data['cpu_freq_mhz'] = int(cpu_freq) // 1000

        # Load average
        loadavg = self.safe_run('cat /proc/loadavg')
        if loadavg:
            parts = loadavg.split()
            if len(parts) >= 3:
                data['load_1min'] = float(parts[0])
                data['load_5min'] = float(parts[1])
                data['load_15min'] = float(parts[2])

        return data


class DeviceProbe(SystemProbe):
    """Probe for device state (battery, temperature, etc)"""

    def probe(self):
        """Collect device metrics"""
        data = {}

        # Battery info (requires termux-api)
        battery_info = self.safe_run('termux-battery-status 2>/dev/null')
        if battery_info:
            try:
                battery_data = json.loads(battery_info)
                data['battery_percent'] = battery_data.get('percentage')
                data['battery_temp'] = battery_data.get('temperature')
                data['battery_status'] = battery_data.get('status')
                data['battery_plugged'] = battery_data.get('plugged')
            except:
                pass

        # Screen state (requires termux-api)
        # Note: This may not be available in all Termux setups

        return data


class ProcessProbe(SystemProbe):
    """Probe for running processes"""

    def probe(self):
        """Collect process metrics"""
        data = {}

        # Get top processes by CPU/memory
        top_output = self.safe_run('top -bn1 -o %CPU | head -n 15')
        if top_output:
            processes = []
            lines = top_output.split('\n')

            # Find header line to determine column positions
            header_idx = -1
            for i, line in enumerate(lines):
                if 'PID' in line and 'CPU' in line:
                    header_idx = i
                    break

            if header_idx >= 0 and header_idx + 1 < len(lines):
                # Parse top 5 processes after header
                for line in lines[header_idx + 1:header_idx + 6]:
                    parts = line.split()
                    if len(parts) >= 9:
                        try:
                            processes.append({
                                'pid': parts[0],
                                'cpu': parts[8] if '%' not in parts[8] else parts[8].replace('%', ''),
                                'name': parts[-1] if len(parts) > 9 else 'unknown'
                            })
                        except:
                            pass

            if processes:
                data['top_processes'] = processes

        # Process count
        ps_count = self.safe_run('ps -A | wc -l')
        if ps_count and ps_count.isdigit():
            data['process_count'] = int(ps_count)

        return data


class CorrelationEngine:
    """Analyze correlations between metrics and anomalies"""

    def __init__(self, spike_threshold=100):
        self.spike_threshold = spike_threshold

    def analyze(self, samples):
        """Perform correlation analysis on samples"""
        if len(samples) < 10:
            return {"error": "Not enough samples for analysis"}

        analysis = {}

        # Identify spikes
        spikes = []
        normal = []

        for i, sample in enumerate(samples):
            net = sample.get('network', {})
            ping = net.get('ping_ms')

            if ping and ping > self.spike_threshold:
                spikes.append((i, sample))
            elif ping:
                normal.append((i, sample))

        analysis['total_samples'] = len(samples)
        analysis['spike_count'] = len(spikes)
        analysis['normal_count'] = len(normal)
        analysis['spike_rate'] = (len(spikes) / len(samples) * 100) if samples else 0

        if not spikes:
            analysis['message'] = "No spikes detected"
            return analysis

        # Analyze conditions during spikes vs normal
        correlations = []

        # CPU correlation
        spike_cpu = [s[1].get('resources', {}).get('cpu_percent')
                     for s in spikes if s[1].get('resources', {}).get('cpu_percent')]
        normal_cpu = [s[1].get('resources', {}).get('cpu_percent')
                      for s in normal if s[1].get('resources', {}).get('cpu_percent')]

        if spike_cpu and normal_cpu:
            avg_spike_cpu = mean(spike_cpu)
            avg_normal_cpu = mean(normal_cpu)
            diff = avg_spike_cpu - avg_normal_cpu

            high_cpu_spikes = sum(1 for c in spike_cpu if c > 70)
            correlation_strength = (high_cpu_spikes / len(spikes) * 100) if spikes else 0

            correlations.append({
                'metric': 'CPU Usage',
                'spike_avg': f"{avg_spike_cpu:.1f}%",
                'normal_avg': f"{avg_normal_cpu:.1f}%",
                'difference': f"{diff:+.1f}%",
                'correlation_pct': correlation_strength,
                'strength': 'STRONG' if correlation_strength > 60 else 'MODERATE' if correlation_strength > 30 else 'WEAK'
            })

        # Memory correlation
        spike_mem = [s[1].get('resources', {}).get('mem_percent')
                     for s in spikes if s[1].get('resources', {}).get('mem_percent')]
        normal_mem = [s[1].get('resources', {}).get('mem_percent')
                      for s in normal if s[1].get('resources', {}).get('mem_percent')]

        if spike_mem and normal_mem:
            avg_spike_mem = mean(spike_mem)
            avg_normal_mem = mean(normal_mem)
            diff = avg_spike_mem - avg_normal_mem

            high_mem_spikes = sum(1 for m in spike_mem if m > 80)
            correlation_strength = (high_mem_spikes / len(spikes) * 100) if spikes else 0

            correlations.append({
                'metric': 'Memory Usage',
                'spike_avg': f"{avg_spike_mem:.1f}%",
                'normal_avg': f"{avg_normal_mem:.1f}%",
                'difference': f"{diff:+.1f}%",
                'correlation_pct': correlation_strength,
                'strength': 'STRONG' if correlation_strength > 60 else 'MODERATE' if correlation_strength > 30 else 'WEAK'
            })

        # Battery correlation
        spike_batt = [s[1].get('device', {}).get('battery_percent')
                      for s in spikes if s[1].get('device', {}).get('battery_percent')]
        normal_batt = [s[1].get('device', {}).get('battery_percent')
                       for s in normal if s[1].get('device', {}).get('battery_percent')]

        if spike_batt and normal_batt:
            avg_spike_batt = mean(spike_batt)
            avg_normal_batt = mean(normal_batt)
            diff = avg_spike_batt - avg_normal_batt

            low_batt_spikes = sum(1 for b in spike_batt if b < 30)
            correlation_strength = (low_batt_spikes / len(spikes) * 100) if spikes else 0

            correlations.append({
                'metric': 'Battery Level',
                'spike_avg': f"{avg_spike_batt:.1f}%",
                'normal_avg': f"{avg_normal_batt:.1f}%",
                'difference': f"{diff:+.1f}%",
                'correlation_pct': correlation_strength,
                'strength': 'STRONG' if correlation_strength > 60 else 'MODERATE' if correlation_strength > 30 else 'WEAK'
            })

        # WiFi signal correlation
        spike_wifi = [s[1].get('network', {}).get('wifi_rssi')
                      for s in spikes if s[1].get('network', {}).get('wifi_rssi')]
        normal_wifi = [s[1].get('network', {}).get('wifi_rssi')
                       for s in normal if s[1].get('network', {}).get('wifi_rssi')]

        if spike_wifi and normal_wifi:
            avg_spike_wifi = mean(spike_wifi)
            avg_normal_wifi = mean(normal_wifi)
            diff = avg_spike_wifi - avg_normal_wifi

            weak_wifi_spikes = sum(1 for w in spike_wifi if w < -60)
            correlation_strength = (weak_wifi_spikes / len(spikes) * 100) if spikes else 0

            correlations.append({
                'metric': 'WiFi Signal',
                'spike_avg': f"{avg_spike_wifi:.1f}dBm",
                'normal_avg': f"{avg_normal_wifi:.1f}dBm",
                'difference': f"{diff:+.1f}dBm",
                'correlation_pct': correlation_strength,
                'strength': 'STRONG' if correlation_strength > 60 else 'MODERATE' if correlation_strength > 30 else 'WEAK'
            })

        # Sort by correlation strength
        correlations.sort(key=lambda x: x['correlation_pct'], reverse=True)
        analysis['correlations'] = correlations

        return analysis


class SystemMonitor:
    """Main orchestrator for comprehensive system monitoring"""

    def __init__(self, interval=30, duration_hours=2, spike_threshold=100, host='8.8.8.8'):
        self.interval = interval
        self.duration = duration_hours * 3600
        self.spike_threshold = spike_threshold
        self.host = host

        # Initialize probes
        self.network_probe = NetworkProbe(host)
        self.resource_probe = ResourceProbe()
        self.device_probe = DeviceProbe()
        self.process_probe = ProcessProbe()

        # Initialize correlation engine
        self.correlation_engine = CorrelationEngine(spike_threshold)

        # Data storage
        self.samples = []
        self.start_time = None
        self.end_time = None

        # Acquire wakelock
        self.acquire_wakelock()

    def acquire_wakelock(self):
        """Acquire Termux wakelock"""
        try:
            subprocess.run(['termux-wake-lock'], check=False)
            print("✓ Wakelock acquired")
        except:
            print("⚠ Could not acquire wakelock")

    def release_wakelock(self):
        """Release wakelock"""
        try:
            subprocess.run(['termux-wake-unlock'], check=False)
            print("✓ Wakelock released")
        except:
            pass

    def send_notification(self, title, message):
        """Send notification"""
        try:
            subprocess.run(
                ['termux-notification', '--title', title, '--content', message],
                check=False
            )
        except:
            pass

    def probe_all(self):
        """Collect all metrics from all probes"""
        sample = {
            'timestamp': datetime.now().isoformat(),
            'network': self.network_probe.probe(),
            'resources': self.resource_probe.probe(),
            'device': self.device_probe.probe(),
            'processes': self.process_probe.probe()
        }
        return sample

    def format_sample_display(self, sample, sample_num):
        """Format sample for display"""
        net = sample.get('network', {})
        res = sample.get('resources', {})
        dev = sample.get('device', {})

        # Ping
        ping = net.get('ping_ms')
        ping_str = f"{ping:.1f}ms" if ping else "FAIL"
        ping_symbol = "✓" if ping and ping < self.spike_threshold else "⚠" if ping else "✗"

        # CPU
        cpu = res.get('cpu_percent')
        cpu_str = f"{cpu:>4.1f}%" if cpu is not None else " N/A"

        # Memory
        mem = res.get('mem_percent')
        mem_str = f"{mem:>3.0f}%" if mem else "N/A"

        # Battery
        batt = dev.get('battery_percent')
        batt_str = f"{batt:>3.0f}%" if batt else "N/A"

        # WiFi
        wifi = net.get('wifi_rssi')
        wifi_str = f"{wifi:>4.0f}dBm" if wifi else "N/A"

        # Elapsed time
        elapsed = (datetime.now() - self.start_time).total_seconds() / 3600

        # Status indicator
        status = "🟢"
        if ping and ping > self.spike_threshold:
            status = "🔴"
        elif cpu and cpu > 80:
            status = "🟡"

        return (f"[{sample_num:4d}] {ping_symbol} {ping_str:>8} | "
                f"CPU:{cpu_str} Mem:{mem_str} Bat:{batt_str} WiFi:{wifi_str} | "
                f"{elapsed:.2f}h {status}")

    def check_anomalies(self, sample, sample_num):
        """Check for anomalies and send alerts"""
        net = sample.get('network', {})
        res = sample.get('resources', {})

        ping = net.get('ping_ms')
        cpu = res.get('cpu_percent')

        # Spike with high CPU
        if ping and ping > self.spike_threshold and cpu and cpu > 70:
            msg = f"Ping spike ({ping:.0f}ms) with high CPU ({cpu:.0f}%)"
            print(f"\n⚠️  CORRELATION: {msg}")
            self.send_notification("Network + CPU Issue", msg)

    def run(self):
        """Run the monitoring session"""
        self.start_time = datetime.now()
        end_time = self.start_time + timedelta(seconds=self.duration)

        print(f"\n{'='*80}")
        print("COMPREHENSIVE SYSTEM MONITOR")
        print(f"{'='*80}")
        print(f"Host: {self.host}")
        print(f"Interval: {self.interval}s")
        print(f"Duration: {self.duration/3600:.1f}h")
        print(f"Spike threshold: {self.spike_threshold}ms")
        print(f"Start: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"End: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"\n🔔 Monitoring: Network | CPU | Memory | Battery | WiFi | Processes")
        print(f"\nTracking... (Ctrl+C to stop)\n")

        sample_num = 0

        try:
            while datetime.now() < end_time:
                sample = self.probe_all()
                self.samples.append(sample)
                sample_num += 1

                # Display
                print(self.format_sample_display(sample, sample_num))

                # Check for anomalies
                self.check_anomalies(sample, sample_num)

                # Sleep
                time.sleep(self.interval)

        except KeyboardInterrupt:
            print("\n\n⚠ Stopped by user")

        self.end_time = datetime.now()
        self.release_wakelock()
        self.save_results()
        self.print_report()

    def save_results(self):
        """Save results to JSON"""
        filename = f"sysmon_{self.start_time.strftime('%Y%m%d_%H%M%S')}.json"

        data = {
            'host': self.host,
            'interval': self.interval,
            'spike_threshold': self.spike_threshold,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat(),
            'samples': self.samples
        }

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"\n✓ Results saved to: {filename}")
        return filename

    def print_report(self):
        """Print comprehensive analysis report"""
        if not self.samples:
            print("No data collected")
            return

        print(f"\n{'='*80}")
        print("SYSTEM MONITOR ANALYSIS REPORT")
        print(f"{'='*80}")

        duration = (self.end_time - self.start_time).total_seconds()
        print(f"\nDuration: {duration/3600:.2f} hours ({len(self.samples)} samples)")

        # Network stats
        pings = [s['network'].get('ping_ms') for s in self.samples
                 if s['network'].get('ping_ms')]

        if pings:
            print(f"\n{'NETWORK STATISTICS':-^80}")
            print(f"Average latency: {mean(pings):.2f}ms")
            print(f"Median latency: {median(pings):.2f}ms")
            print(f"Min/Max: {min(pings):.2f}ms / {max(pings):.2f}ms")
            print(f"Std deviation: {stdev(pings):.2f}ms")

            spikes = [p for p in pings if p > self.spike_threshold]
            print(f"Spikes (>{self.spike_threshold}ms): {len(spikes)} ({len(spikes)/len(pings)*100:.1f}%)")

        # Resource stats
        cpus = [s['resources'].get('cpu_percent') for s in self.samples
                if s['resources'].get('cpu_percent')]
        mems = [s['resources'].get('mem_percent') for s in self.samples
                if s['resources'].get('mem_percent')]

        if cpus or mems:
            print(f"\n{'RESOURCE STATISTICS':-^80}")
            if cpus:
                print(f"CPU: avg={mean(cpus):.1f}% median={median(cpus):.1f}% "
                      f"max={max(cpus):.1f}%")
            if mems:
                print(f"Memory: avg={mean(mems):.1f}% median={median(mems):.1f}% "
                      f"max={max(mems):.1f}%")

        # Battery stats
        batts = [s['device'].get('battery_percent') for s in self.samples
                 if s['device'].get('battery_percent')]

        if batts:
            print(f"\n{'BATTERY STATISTICS':-^80}")
            print(f"Range: {min(batts):.0f}% to {max(batts):.0f}%")
            print(f"Average: {mean(batts):.1f}%")

        # Correlation analysis
        print(f"\n{'CORRELATION ANALYSIS':-^80}")
        analysis = self.correlation_engine.analyze(self.samples)

        if 'correlations' in analysis and analysis['correlations']:
            print(f"Analyzing {analysis['spike_count']} spikes out of {analysis['total_samples']} samples...\n")

            for i, corr in enumerate(analysis['correlations'], 1):
                strength_emoji = "🔴" if corr['strength'] == 'STRONG' else "🟡" if corr['strength'] == 'MODERATE' else "🟢"
                print(f"{i}. {corr['metric']}: {strength_emoji} {corr['strength']}")
                print(f"   During spikes: {corr['spike_avg']} (avg)")
                print(f"   During normal: {corr['normal_avg']} (avg)")
                print(f"   Difference: {corr['difference']}")
                print(f"   Correlation: {corr['correlation_pct']:.1f}% of spikes\n")
        else:
            print(analysis.get('message', 'No significant correlations found'))

        print(f"{'='*80}\n")


def main():
    print("\n" + "="*80)
    print("COMPREHENSIVE SYSTEM MONITOR")
    print("Network + CPU + Memory + Battery + WiFi + Process Tracking")
    print("="*80 + "\n")

    try:
        host = input("Host to ping [default: 8.8.8.8]: ").strip() or "8.8.8.8"

        interval_input = input("Sample interval in seconds [default: 30]: ").strip()
        interval = int(interval_input) if interval_input else 30

        duration_input = input("Duration in hours [default: 2]: ").strip()
        duration = float(duration_input) if duration_input else 2

        threshold_input = input("Spike threshold in ms [default: 100]: ").strip()
        threshold = int(threshold_input) if threshold_input else 100

        monitor = SystemMonitor(
            interval=interval,
            duration_hours=duration,
            spike_threshold=threshold,
            host=host
        )

        monitor.run()

    except KeyboardInterrupt:
        print("\n\nExiting...")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
