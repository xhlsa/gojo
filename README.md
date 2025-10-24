# Gojo: Sensor Fusion & System Monitoring Playground

A collection of sensor fusion, motion tracking, and system monitoring experiments. Single working directory for all Termux projects.

**Priority Tool:** Motion Tracker V2 (production-ready)
**Status:** General playground for various experiments

## 🎯 Quick Start

### Motion Tracker V2 (Production Ready)
Track vehicle motion using GPS + accelerometer sensor fusion:

```bash
# Run continuous (until Ctrl+C)
python motion_tracker_v2/motion_tracker_v2.py

# Run for N minutes
python motion_tracker_v2/motion_tracker_v2.py 5

# Or use wrapper script
./motion_tracker_v2.sh 10
```

Data saves to `motion_tracker_sessions/` with JSON, compressed, and GPX formats.

---

## 📁 Key Files

| Path | Purpose |
|------|---------|
| `motion_tracker_v2/` | Main motion tracking application |
| `motion_tracker_sessions/` | Session data storage |
| `.claude/CLAUDE.md` | Detailed technical notes & code patterns |
| `motion_tracker_v2.sh` | Convenient launcher script |

---

## ✨ Features

- **Complementary Filtering:** Fuses GPS (accurate, low-freq) + accel (noisy, high-freq)
- **Cython Optimization:** 25x faster math, 70% CPU reduction (optional)
- **Dynamic Re-calibration:** Auto-corrects for phone rotation during stops
- **Memory Bounded:** Auto-saves every 2 minutes, clears old data
- **Battery Tracking:** Logs battery status during sessions
- **Multiple Formats:** JSON, compressed .gz, GPX for map apps

---

## 🔍 For Next Session

See `.claude/CLAUDE.md` for:
- Complete technical overview
- 6 reusable code patterns (with file references)
- Design decisions & tuning parameters
- Future improvement ideas

---

## 📊 Last Session (Oct 23)

✓ Added dynamic re-calibration
✓ 3 test runs - all passing
✓ Ready for real-world drive session

Latest data: `motion_tracker_sessions/motion_track_v2_20251023_205116.*`

---

## 📦 Other Tools in This Workspace

| Tool | Purpose | Status |
|------|---------|--------|
| `motion_tracker.py` | Original motion tracker (v1) | Legacy |
| `motion_tracker_benchmark.py` | Performance testing & benchmarking | Utility |
| `system_monitor.py` | Termux system stats & telemetry | Active |
| `ping_tracker.py` | Network ping tracking | Utility |
| `ping_tracker_enhanced.py` | Enhanced ping analysis | Utility |
| `gps_tester.py` | GPS functionality validation | Testing |
| `monitor_ping.sh` | Simple ping monitoring script | Utility |
| `data/` | Archive folder for old test data | Archive |
| `docs/` | Documentation | Reference |

**Philosophy:** Single directory keeps related Termux projects together. Each can be developed/tested independently.

---

## 🛠️ Development Notes

**Priority Project:** Motion Tracker V2
**Status:** Production ready with dynamic calibration
**Testing:** Tested on 3min, 2min, 5min runs (indoor & highway)
**Next Step:** Validate dynamic recal during actual traffic stops

For detailed technical patterns and context → see `.claude/CLAUDE.md`
