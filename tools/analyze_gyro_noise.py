
import time
import json
import subprocess
import numpy as np
from collections import deque

def collect_gyro_data(duration=30):
    """
    Collects gyroscope data for a specified duration by running a shell script.
    """
    print(f"Collecting gyroscope data for {duration} seconds...")

    script_path = './tools/collect_raw_gyro_data.sh'
    output_file = 'gyro_data.json'

    # Run the shell script with timeout (duration + 60s buffer for init/cleanup/retries)
    # Includes: validation (5s) + cleanup (5s) + retries (3-9s) + pause (2s) + collection (duration)
    timeout_seconds = duration + 60
    try:
        subprocess.run([script_path, str(duration)], text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        print(f"⚠ Warning: Collection script exceeded {timeout_seconds}s timeout (may be stuck)")
        return {}
    except Exception as e:
        print(f"Error running collection script: {e}")
        return {}

    gyro_data = {'x': [], 'y': [], 'z': []}

    try:
        with open(output_file, 'r') as f:
            file_content = f.read()

        if not file_content.strip():
            print("⚠ Warning: No gyroscope data in output file")
            return {}

        # Parse JSON objects from the stream
        # termux-sensor outputs multiple JSON objects in pretty-printed (multi-line) format
        lines = file_content.strip().split('\n')
        json_buffer = ""
        brace_depth = 0

        for line in lines:
            json_buffer += line + '\n'
            brace_depth += line.count('{') - line.count('}')

            # When braces are balanced and we have at least one complete object, parse it
            if brace_depth == 0 and json_buffer.count('{') > 0:
                try:
                    data = json.loads(json_buffer)
                    json_buffer = ""

                    if not isinstance(data, dict) or not data:
                        # Skip empty dicts and non-dicts
                        continue

                    # Find the sensor key dynamically (contains "Gyroscope")
                    sensor_key = None
                    for key in data:
                        if "Gyroscope" in key or "gyroscope" in key.lower():
                            sensor_key = key
                            break

                    if sensor_key and 'values' in data[sensor_key]:
                        values = data[sensor_key]['values']
                        if len(values) >= 3:
                            # Convert scientific notation to float
                            gyro_data['x'].append(float(values[0]))
                            gyro_data['y'].append(float(values[1]))
                            gyro_data['z'].append(float(values[2]))
                except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                    # Skip malformed JSON, continue
                    json_buffer = ""
                    brace_depth = 0
                    continue

    except FileNotFoundError:
        print(f"Error: Output file not found at {output_file}")
        return {}
    except Exception as e:
        print(f"Error reading output file: {e}")
        return {}

    if not gyro_data['x']:
        print("⚠ Warning: No valid gyroscope samples found")
        return {}

    print(f"✓ Collected {len(gyro_data['x'])} samples.")
    return gyro_data

def analyze_gyro_noise(data):
    """
    Analyzes the collected gyroscope data to estimate measurement noise and bias drift.
    """
    if not data or not data.get('x'):
        print("⚠ No data collected, cannot analyze.")
        return

    print("\n--- Gyroscope Noise Analysis ---")

    # --- Measurement Noise (R) ---
    var_x = np.var(data['x'])
    var_y = np.var(data['y'])
    var_z = np.var(data['z'])
    
    print(f"Variance (Measurement Noise R):")
    print(f"  x: {var_x:.8f}")
    print(f"  y: {var_y:.8f}")
    print(f"  z: {var_z:.8f}")
    
    # The gyro_noise_std in the EKF is the standard deviation
    std_x = np.sqrt(var_x)
    std_y = np.sqrt(var_y)
    std_z = np.sqrt(var_z)
    
    print(f"\nStandard Deviation (for EKF's gyro_noise_std):")
    print(f"  x: {std_x:.8f}")
    print(f"  y: {std_y:.8f}")
    print(f"  z: {std_z:.8f}")
    
    # --- Bias Drift (for Q) ---
    n_samples = len(data['x'])
    if n_samples < 20:
        print("\nNot enough samples to analyze bias drift.")
        return

    # Split data into two halves
    mid_point = n_samples // 2
    
    mean_first_half_x = np.mean(data['x'][:mid_point])
    mean_second_half_x = np.mean(data['x'][mid_point:])
    
    mean_first_half_y = np.mean(data['y'][:mid_point])
    mean_second_half_y = np.mean(data['y'][mid_point:])
    
    mean_first_half_z = np.mean(data['z'][:mid_point])
    mean_second_half_z = np.mean(data['z'][mid_point:])

    drift_x = abs(mean_second_half_x - mean_first_half_x)
    drift_y = abs(mean_second_half_y - mean_first_half_y)
    drift_z = abs(mean_second_half_z - mean_first_half_z)

    print("\nBias Drift Analysis (change in mean over time):")
    print(f"  x-axis drift: {drift_x:.8f} rad/s")
    print(f"  y-axis drift: {drift_y:.8f} rad/s")
    print(f"  z-axis drift: {drift_z:.8f} rad/s")

    # The q_bias parameter is the standard deviation of the bias random walk process.
    # A reasonable estimate for q_bias is on the order of the observed drift.
    # We can take the max drift as a conservative estimate.
    max_drift = max(drift_x, drift_y, drift_z)
    
    print(f"\nRecommended `q_bias` value (process noise):")
    print(f"  Based on max drift: {max_drift:.8f}")
    print("  (This is a starting point, may need further tuning)")


if __name__ == "__main__":
    gyro_data = collect_gyro_data(30)
    analyze_gyro_noise(gyro_data)
