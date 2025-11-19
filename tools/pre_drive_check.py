#!/usr/bin/env python3
"""
Quick pre-drive checklist to verify the Termux environment before running test_ekf.sh.

The script is intentionally lightweight so it can run on-device and highlight the most
common issues (missing binaries, stale sessions directory, etc.) before a real drive.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_DIR = REPO_ROOT / "motion_tracker_sessions"
REQUIRED_SCRIPTS = [
    "motion_tracker_v2.sh",
    "test_ekf.sh",
    "start_dashboard.sh",
]
REQUIRED_BINARIES = [
    "termux-sensor",
    "termux-location",
]


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


def _format_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def check_scripts() -> Iterable[CheckResult]:
    for script in REQUIRED_SCRIPTS:
        path = (REPO_ROOT / script).resolve()
        if path.exists() and os.access(path, os.X_OK):
            detail = f"{script} ✅ ({path})"
            yield CheckResult(f"{script} executable", True, detail)
        elif path.exists():
            yield CheckResult(
                f"{script} executable",
                False,
                f"{script} exists but is not executable ({path})",
            )
        else:
            yield CheckResult(
                f"{script} executable",
                False,
                f"{script} missing ({path})",
            )


def check_binaries() -> Iterable[CheckResult]:
    for binary in REQUIRED_BINARIES:
        located = shutil.which(binary)
        if located:
            yield CheckResult(f"{binary} available", True, f"Found at {located}")
        else:
            yield CheckResult(
                f"{binary} available",
                False,
                "Not found in PATH. Install via pkg/Termux extras.",
            )


def check_sessions_dir() -> CheckResult:
    if not SESSIONS_DIR.exists():
        return CheckResult("Sessions directory", False, f"{SESSIONS_DIR} not found")
    if not SESSIONS_DIR.is_dir():
        return CheckResult("Sessions directory", False, f"{SESSIONS_DIR} is not a dir")

    files: List[Path] = sorted(
        SESSIONS_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return CheckResult(
            "Sessions directory",
            True,
            f"{SESSIONS_DIR} exists but has no *.json files yet",
        )

    latest = files[0]
    timestamp = _format_timestamp(latest.stat().st_mtime)
    return CheckResult(
        "Sessions directory",
        True,
        f"Latest session: {latest.name} ({timestamp})",
    )


def check_disk_space() -> CheckResult:
    usage = shutil.disk_usage(REPO_ROOT)
    free_gb = usage.free / (1024 ** 3)
    detail = f"{free_gb:.2f} GB free under {REPO_ROOT}"
    return CheckResult("Disk space", free_gb > 1.0, detail)


def python_version() -> CheckResult:
    try:
        output = subprocess.check_output(
            ["python3", "--version"],
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
        return CheckResult("Python version", True, output)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return CheckResult("Python version", False, f"python3 unavailable ({exc})")


def summarize(results: Iterable[CheckResult]) -> None:
    print("=== Pre-Drive Checklist ===")
    failures = 0
    for result in results:
        status = "OK" if result.passed else "!!"
        print(f"[{status}] {result.name}: {result.detail}")
        if not result.passed:
            failures += 1

    if failures:
        print(f"\n⚠️  {failures} issue(s) detected. Resolve before running test_ekf.sh.")
    else:
        print("\nAll checks passed. You're ready for tomorrow's drive.")


def main() -> None:
    checks: List[CheckResult] = []
    checks.extend(check_scripts())
    checks.extend(check_binaries())
    checks.append(check_sessions_dir())
    checks.append(check_disk_space())
    checks.append(python_version())
    summarize(checks)


if __name__ == "__main__":
    main()
