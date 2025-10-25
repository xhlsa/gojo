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

## 📁 Project Structure

```
gojo/
├── motion_tracker_v2/              ⭐ Main application (production-ready)
├── motion_tracker_kalman/          Kalman filter experiment
├── motion_tracker_sessions/        Session data storage (JSON, GZ, GPX)
├── tools/                          Legacy & utility scripts
├── tests/                          Test & analysis files
├── docs/                           Documentation & references
├── motion_tracker_v2.sh            Launcher wrapper
└── .claude/CLAUDE.md               Technical patterns & session notes
```

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

## 📦 Additional Tools

In `tools/` - Legacy & utility scripts:
- `motion_tracker.py` - Original v1 (reference)
- `system_monitor.py` - Termux system stats
- `ping_tracker.py` / `ping_tracker_enhanced.py` - Network monitoring
- `gps_tester.py` - GPS validation
- `monitor_ping.sh` - Ping monitoring script

In `tests/` - Test & analysis utilities:
- `motion_tracker_benchmark.py` - Performance testing
- Various sensor daemon & accel tests
- `analyze_drive.py` - Session data analysis

**Philosophy:** Single directory keeps related Termux projects together. Each can be developed/tested independently.

---

## 🛠️ Development Notes

**Priority Project:** Motion Tracker V2
**Status:** Production ready with dynamic calibration
**Testing:** Tested on 3min, 2min, 5min runs (indoor & highway)
**Next Step:** Validate dynamic recal during actual traffic stops

For detailed technical patterns and context → see `.claude/CLAUDE.md`
