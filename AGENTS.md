# Repository Guidelines

## Reference Links
- [README.md](README.md) – product overview, deployment shortcuts, and current status.
- [.claude/CLAUDE.md](.claude/CLAUDE.md) – deep dive on incident detection, memory fixes, and session notes.
- [GEMINI.md](GEMINI.md) – automation-focused handoff for Gemini agents that mirrors the docs here.
- Primary runtime harness: `./test_ekf.sh` (always prefer this script unless documenting the lighter `motion_tracker_v2.sh` path).

## Project Structure & Module Organization
Core sensor-fusion code lives in `motion_tracker_v2/` (filters, incident detector, Cython acceleration, docs). Shell wrappers such as `motion_tracker_v2.sh`, `test_ekf.sh`, and `run_load_test.sh` prepare sensors and are the only supported entrypoints. Bench and automation utilities stay in `tests/`, while on-device smoke/regression helpers live as top-level `test_*.sh` or `.py`. Session output persists in `motion_tracker_sessions/`, failures roll into `crash_logs/`, and investigation notebooks belong in `tools/` or `docs/`.

## Build, Test, and Development Commands
- `./test_ekf.sh 10` – canonical workflow (sensor cleanup, EKF vs complementary metrics, crash logging) and default starting point for agents.
- `./motion_tracker_v2.sh 30` – lightweight production tracker; run only when you explicitly need the stripped-down session without analyzer output.
- `python3 motion_tracker_v2/setup.py build_ext --inplace` – compile `accel_processor.pyx` so `FastAccelProcessor` is available; rerun after editing the Cython file or upgrading toolchains.
- `python3 motion_tracker_v2/analyze_comparison.py motion_tracker_sessions/comparison_2025-10-30_*.json` – turn captured sessions into drift/velocity metrics for PR evidence.
- `python3 tests/motion_tracker_snapshot_test.py` – lab-only memory/queue pressure diagnostic; run while connected to mock sensors or playback data.

## Coding Style & Naming Conventions
Follow PEP 8 with 4-space indents, snake_case names, and docstrings that capture hardware assumptions or threading rules. Prefer `f""` strings, `pathlib`, and guarded optional imports (`orjson`, `psutil`, `FastAccelProcessor`) instead of bare try/except prints. Keep streaming I/O buffered (see `PersistentAccelDaemon`) and favor explicit feature flags (e.g., `HAS_CYTHON`) over implicit behavior. For `accel_processor.pyx`, keep APIs identical to the Python fallback and avoid heap allocations inside tight loops.

## Testing Guidelines
Name deterministic lab tests `tests/test_<purpose>.py` and gate hardware-required suites behind shell wrappers so cleanup runs first. Before opening a PR, complete at least one on-device pass of `./test_ekf.sh 5` plus a diagnostic such as `python3 tests/motion_tracker_snapshot_test.py`. Attach resulting `motion_tracker_sessions/*.json`, analyzer output, and any `crash_logs/test_*.log` when reporting behavior. Document skipped tests (e.g., GPS unavailable) directly in the PR description.

## Commit & Pull Request Guidelines
History shows terse, scope-prefixed subjects (`Fix: PR #3 bugs`, `Persist autosave data in durable chunks`). Keep subjects under ~70 characters, start with the impacted component, and describe the change in imperative mood. Bodies should list motivation, metrics, and commands executed (`./motion_tracker_v2.sh 30`, analyzer scripts, etc.). PRs must include: summary, before/after metrics, linked issue or session ID, reproduction commands, and screenshots/log excerpts whenever sensor output formatting changes.
