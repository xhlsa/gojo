#!/data/data/com.termux/files/usr/bin/python3
import gzip
import json
from pathlib import Path
import statistics
import argparse

def load_session(path: Path):
    if path.suffix == '.gz':
        with gzip.open(path, 'rt') as f:
            return json.load(f)
    return json.loads(path.read_text())

def sliding_windows(samples, key, window=2.0):
    # samples sorted by timestamp
    out = []
    n = len(samples)
    left = 0
    for right in range(n):
        while samples[right]['timestamp'] - samples[left]['timestamp'] > window:
            left += 1
        window_samples = samples[left:right+1]
        values = [s[key] for s in window_samples]
        if len(values) >= 2:
            mean = sum(values)/len(values)
            std = statistics.pstdev(values)
        else:
            mean = values[0] if values else 0
            std = 0.0
        out.append((samples[right]['timestamp'], mean, std))
    return out


def classify_hand_motion(gps, accel, gyro, window=2.0,
                          speed_thresh=1.0,
                          gyro_std_thresh=0.1,
                          accel_std_thresh=0.05):
    gps_windows = sliding_windows(gps, 'speed', window)
    accel_windows = sliding_windows(accel, 'magnitude', window)
    gyro_windows = sliding_windows(gyro, 'magnitude', window)

    events = []
    # Align by timestamp of samples (approx). We'll just traverse gyro windows.
    # For each gyro window, find corresponding gps and accel window with nearest timestamp.
    def nearest(windows, t):
        best = None
        best_diff = None
        for ts, mean, std in windows:
            diff = abs(ts - t)
            if best is None or diff < best_diff:
                best = (ts, mean, std)
                best_diff = diff
        return best

    for ts, g_mean, g_std in gyro_windows:
        gps_ts, gps_mean, _ = nearest(gps_windows, ts)
        accel_ts, accel_mean, accel_std = nearest(accel_windows, ts)
        if gps_mean is None:
            continue
        is_hand = gps_mean < speed_thresh and (g_std > gyro_std_thresh or accel_std > accel_std_thresh)
        events.append({
            'timestamp': ts,
            'hand_motion': is_hand,
            'gps_speed_mean': gps_mean,
            'gyro_std': g_std,
            'accel_std': accel_std,
        })
    return events


def summarize(events):
    total = len(events)
    hand = sum(1 for e in events if e['hand_motion'])
    return {
        'total_windows': total,
        'hand_windows': hand,
        'hand_percent': (hand / total * 100.0) if total else 0.0,
        'first_hand_ts': next((e['timestamp'] for e in events if e['hand_motion']), None),
        'last_hand_ts': next((e['timestamp'] for e in reversed(events) if e['hand_motion']), None)
    }


def main():
    parser = argparse.ArgumentParser(description='Analyze hand motion in a session JSON')
    parser.add_argument('session_path', type=Path, help='Path to comparison JSON or JSON.gz file')
    args = parser.parse_args()

    data = load_session(args.session_path)
    gps = data['gps_samples']
    accel = data['accel_samples']
    gyro = data['gyro_samples']

    events = classify_hand_motion(gps, accel, gyro)
    summary = summarize(events)

    print('Hand motion summary:')
    for k, v in summary.items():
        print(f'  {k}: {v}')

    print('\nSample events:')
    for e in events[:: max(1, len(events)//10) ]:
        print(e)

if __name__ == '__main__':
    main()
