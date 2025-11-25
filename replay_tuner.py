import gzip
import json
import math
import pathlib

def load_log(path: pathlib.Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as fh:
            return json.load(fh)
    with open(path, "r") as fh:
        return json.load(fh)

def gps_speed_series(readings):
    return [(r["timestamp"], r["gps"]["speed"])
            for r in readings
            if isinstance(r, dict) and r.get("gps") and r["gps"].get("speed") is not None]

def stats_from_series(series):
    if not series:
        return {}
    vals = [v for _, v in series]
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    def pct(p): return vals_sorted[int(p * n) - 1]
    return {
        "count": n,
        "max": max(vals_sorted),
        "mean": sum(vals_sorted) / n,
        "median": vals_sorted[n // 2],
        "p95": pct(0.95),
        "p99": pct(0.99),
    }

if __name__ == "__main__":
    path = pathlib.Path("motion_tracker_sessions/comparison_20251125_005350.json.gz")
    data = load_log(path)
    gps_series = gps_speed_series(data.get("readings", []))
    traj_series = [(t["timestamp"], t.get("ekf_velocity", 0.0))
                   for t in data.get("trajectories", []) if t.get("ekf_velocity") is not None]

    print("GPS speed stats:", stats_from_series(gps_series))
    print("EKF velocity stats:", stats_from_series(traj_series))

    print("This script is a placeholder for a replay-based tuner. Next steps:")
    print("- Instantiate the 15D EKF with parameter sweeps (q_vel, R_gps_vel, coupling scale)")
    print("- Feed readings in timestamp order, capture EKF velocity trace per param set")
    print("- Compute RMSE vs GPS speed and max spike; rank and report best params")

