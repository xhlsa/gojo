# Filter Module - Quick Start

## Usage

### 1. Use Default Complementary Filter

```bash
python motion_tracker_v2.py 10      # 10 minutes, complementary filter
python motion_tracker_v2.py --test  # Test run, complementary filter
```

### 2. Use Kalman Filter

```bash
python motion_tracker_v2.py 10 --filter=kalman           # 10 minutes
python motion_tracker_v2.py 10 --filter=kalman --test    # Test run
```

### 3. Invalid Filter (Falls Back to Complementary)

```bash
python motion_tracker_v2.py 10 --filter=invalid
# ⚠ Unknown filter type: invalid
#    Use: --filter=complementary or --filter=kalman
#    Defaulting to: complementary
```

---

## What Gets Recorded

Session metadata includes `filter_type`:

```json
{
  "start_time": "2025-10-27T23:41:51",
  "config": {
    "accel_sample_rate": 20,
    "auto_save_interval": 120,
    "filter_type": "kalman"  // ← Which filter was used
  }
}
```

---

## Code Integration

### Instantiate a Filter

```python
from filters import get_filter

# Complementary filter (always available)
fusion = get_filter('complementary')

# Kalman filter (requires numpy, filterpy)
try:
    fusion = get_filter('kalman')
except ImportError:
    print("Install: pip install numpy filterpy")
```

### Use the Filter

```python
# Update with GPS
velocity, distance = fusion.update_gps(
    latitude=37.7749,
    longitude=-122.4194,
    gps_speed=10.0,
    gps_accuracy=5.0
)

# Update with accelerometer
velocity, distance = fusion.update_accelerometer(0.5)

# Get state
state = fusion.get_state()
print(f"Velocity: {state['velocity']} m/s")
print(f"Distance: {state['distance']} m")
print(f"Stationary: {state['is_stationary']}")
```

---

## Comparing Filters

Run identical traces with both filters:

```bash
# Run 1: Complementary (baseline)
python motion_tracker_v2.py 10 > /tmp/comp_output.txt

# Run 2: Kalman (alternative)
python motion_tracker_v2.py 10 --filter=kalman > /tmp/kalman_output.txt

# Check which filter each session used
gunzip -c sessions/*/motion_track_v2_*.json.gz | \
  python3 -c "import sys, json; d=json.load(sys.stdin); \
  print(d['config']['filter_type'])"
```

---

## Adding a New Filter

1. Create `motion_tracker_v2/filters/myfilter.py`:

```python
from .base import SensorFusionBase

class MyFilter(SensorFusionBase):
    def update_gps(self, latitude, longitude, gps_speed=None, gps_accuracy=None):
        # Your implementation
        return self.velocity, self.distance

    def update_accelerometer(self, accel_magnitude):
        # Your implementation
        return self.velocity, self.distance

    def get_state(self):
        # Your implementation
        return {
            'velocity': self.velocity,
            'distance': self.distance,
            'is_stationary': self.is_stationary,
            'last_gps_time': self.last_gps_time
        }
```

2. Register in `motion_tracker_v2/filters/__init__.py`:

```python
def get_filter(filter_type='complementary', **kwargs):
    if filter_type == 'complementary':
        from .complementary import ComplementaryFilter
        return ComplementaryFilter(**kwargs)
    elif filter_type == 'kalman':
        from .kalman import KalmanFilter
        return KalmanFilter(**kwargs)
    elif filter_type == 'myfilter':  # ← Add this
        from .myfilter import MyFilter
        return MyFilter(**kwargs)
    else:
        raise ValueError(f"Unknown filter type: {filter_type}")
```

3. Use it:

```bash
python motion_tracker_v2.py 10 --filter=myfilter
```

That's it! No changes to main tracker code needed.

---

## Troubleshooting

### Kalman Filter Not Available

```
ImportError: filterpy module not found
```

**Fix:**
```bash
pip install numpy filterpy
```

### Filter Type Not Recognized

```
⚠ Unknown filter type: xyz
```

**Fix:** Use one of:
- `--filter=complementary` (default)
- `--filter=kalman`

### Comparing Sessions

Check which filter each session used:

```bash
for file in sessions/*/motion_track_v2_*.json.gz; do
  echo -n "$file: "
  gunzip -c "$file" | python3 -c "import sys,json; print(json.load(sys.stdin)['config']['filter_type'])"
done
```
