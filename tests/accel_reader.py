#!/usr/bin/env python3
"""
Minimal accelerometer reader for Termux
Lifecycle: awaken (list/verify) -> read (collect samples) -> shutdown (cleanup)
"""

import subprocess
import json
import sys
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class AccelSample:
    """Single accelerometer reading"""
    x: float
    y: float
    z: float
    timestamp: int

    def __str__(self):
        return f"x={self.x:.3f} y={self.y:.3f} z={self.z:.3f} t={self.timestamp}"


class AccelerometerReader:
    """Handle accelerometer lifecycle: init -> read -> shutdown"""

    def __init__(self, sensor_name: str = "accel"):
        self.sensor_name = sensor_name
        self.available_sensors = []
        self.is_ready = False
        self.samples = []

    def awaken(self) -> bool:
        """
        Initialize: List available sensors and verify accelerometer exists
        Returns True if accel sensor found
        """
        print(f"[AWAKEN] Listing available sensors...")

        try:
            output = subprocess.check_output(
                "termux-sensor -l",
                shell=True,
                text=True
            )

            lines = output.strip().split('\n')
            self.available_sensors = [line.strip() for line in lines if line.strip()]

            print(f"[AWAKEN] Found {len(self.available_sensors)} sensor(s):")
            for sensor in self.available_sensors:
                print(f"  - {sensor}")

            # Check if accelerometer is available
            accel_found = any(self.sensor_name.lower() in s.lower()
                            for s in self.available_sensors)

            if accel_found:
                print(f"[AWAKEN] ✓ Accelerometer sensor found")
                self.is_ready = True
                return True
            else:
                print(f"[AWAKEN] ✗ Accelerometer sensor NOT found")
                return False

        except subprocess.CalledProcessError as e:
            print(f"[AWAKEN] Error listing sensors: {e}")
            return False
        except Exception as e:
            print(f"[AWAKEN] Unexpected error: {e}")
            return False

    def read(self, num_samples: int = 10, interval_ms: int = 50) -> List[AccelSample]:
        """
        Read N accelerometer samples at specified interval
        Returns list of AccelSample objects

        Note: termux-sensor outputs multiline pretty-printed JSON, one object per line
        """
        if not self.is_ready:
            print("[READ] Error: Sensor not ready. Call awaken() first.")
            return []

        print(f"[READ] Reading {num_samples} samples at {interval_ms}ms interval...")
        self.samples = []

        cmd = f"termux-sensor -s {self.sensor_name} -n {num_samples} -d {interval_ms}"

        try:
            # Run as subprocess, capture output
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Read and parse JSON objects
            buffer = ""
            brace_count = 0

            for line in proc.stdout:
                buffer += line
                brace_count += line.count('{') - line.count('}')

                # Complete JSON object when braces match
                if brace_count == 0 and buffer.strip():
                    try:
                        data = json.loads(buffer)

                        # Extract sensor name (first key) and values
                        sensor_key = list(data.keys())[0]
                        values = data[sensor_key].get('values', [])

                        if len(values) >= 3:
                            sample = AccelSample(
                                x=values[0],
                                y=values[1],
                                z=values[2],
                                timestamp=0
                            )
                            self.samples.append(sample)
                            print(f"  {len(self.samples)}: {sample}")

                        buffer = ""

                    except (json.JSONDecodeError, KeyError, IndexError) as e:
                        print(f"  [SKIP] Parse error: {str(e)[:40]}")
                        buffer = ""

            proc.wait(timeout=60)

            print(f"[READ] ✓ Successfully read {len(self.samples)} samples")
            return self.samples

        except subprocess.TimeoutExpired:
            print(f"[READ] Error: Command timeout after 60s")
            proc.kill()
            return []
        except Exception as e:
            print(f"[READ] Unexpected error: {e}")
            return []

    def shutdown(self) -> None:
        """
        Cleanup: Reset state, clear samples from memory if needed
        """
        print(f"[SHUTDOWN] Cleaning up...")

        # Reset state
        self.is_ready = False

        # Optionally clear large data
        sample_count = len(self.samples)
        self.samples = []

        print(f"[SHUTDOWN] ✓ Cleared {sample_count} samples from memory")
        print(f"[SHUTDOWN] ✓ Sensor shutdown complete")

    def get_stats(self) -> dict:
        """Return basic statistics about collected samples"""
        if not self.samples:
            return {"count": 0}

        xs = [s.x for s in self.samples]
        ys = [s.y for s in self.samples]
        zs = [s.z for s in self.samples]

        return {
            "count": len(self.samples),
            "x_mean": sum(xs) / len(xs),
            "y_mean": sum(ys) / len(ys),
            "z_mean": sum(zs) / len(zs),
            "x_min": min(xs),
            "x_max": max(xs),
            "z_min": min(zs),
            "z_max": max(zs),
        }


def main():
    """Example lifecycle: awaken -> read -> shutdown"""

    reader = AccelerometerReader(sensor_name="accel")

    # Step 1: AWAKEN
    if not reader.awaken():
        print("\n[FATAL] Could not initialize accelerometer")
        sys.exit(1)

    print()

    # Step 2: READ
    reader.read(num_samples=10, interval_ms=200)

    print()

    # Step 3: STATS (optional)
    stats = reader.get_stats()
    print(f"[STATS] {stats}")

    print()

    # Step 4: SHUTDOWN
    reader.shutdown()


if __name__ == "__main__":
    main()
