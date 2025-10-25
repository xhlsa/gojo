
import json
import sys
from datetime import datetime

def analyze_gpx_data(file_path):
    with open(file_path, 'r') as f:
        data = json.load(f)

    start_time = datetime.fromisoformat(data['start_time'])
    end_time = datetime.fromisoformat(data['end_time'])
    duration = end_time - start_time

    total_distance_km = data['total_distance'] / 1000.0
    total_distance_miles = total_distance_km * 0.621371

    if duration.total_seconds() > 0:
        average_speed_kmh = total_distance_km / (duration.total_seconds() / 3600)
    else:
        average_speed_kmh = 0
    average_speed_mph = average_speed_kmh * 0.621371

    max_speed_kmh = 0
    elevation_gain = 0
    elevation_loss = 0
    last_elevation = None

    for sample in data['gps_samples']:
        if sample.get('gps'):
            speed = sample['gps'].get('speed', 0) * 3.6  # m/s to km/h
            if speed > max_speed_kmh:
                max_speed_kmh = speed

            altitude = sample['gps'].get('altitude')
            if altitude is not None:
                if last_elevation is not None:
                    diff = altitude - last_elevation
                    if diff > 0:
                        elevation_gain += diff
                    else:
                        elevation_loss += abs(diff)
                last_elevation = altitude
    
    max_speed_mph = max_speed_kmh * 0.621371
    elevation_gain_feet = elevation_gain * 3.28084
    elevation_loss_feet = elevation_loss * 3.28084

    print(f"Driving Metrics for: {file_path}")
    print("-" * 30)
    print(f"Total Distance: {total_distance_km:.2f} km ({total_distance_miles:.2f} miles)")
    print(f"Duration: {str(duration)}")
    print(f"Average Speed: {average_speed_kmh:.2f} km/h ({average_speed_mph:.2f} mph)")
    print(f"Maximum Speed: {max_speed_kmh:.2f} km/h ({max_speed_mph:.2f} mph)")
    print(f"Elevation Gain: {elevation_gain:.2f} m ({elevation_gain_feet:.2f} ft)")
    print(f"Elevation Loss: {elevation_loss:.2f} m ({elevation_loss_feet:.2f} ft)")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python analyze_drive.py <file_path>")
        sys.exit(1)
    analyze_gpx_data(sys.argv[1])
