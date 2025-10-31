#!/usr/bin/env python3
"""
Crash Logger & Session Tracker
Provides structured logging for test crashes and session recovery
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any


class CrashLogger:
    """
    Logs crashes and test sessions with full context.
    Enables reconstruction of what was happening when things broke.
    """

    def __init__(self, session_dir: str = "crash_logs"):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(exist_ok=True)

        self.session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_file = self.session_dir / f"session_{self.session_id}.json"
        self.crash_file = self.session_dir / f"crash_{self.session_id}.log"

        self.session_data = {
            "session_id": self.session_id,
            "started_at": datetime.now().isoformat(),
            "test_name": None,
            "test_args": None,
            "status": "running",
            "events": [],
            "last_output_lines": []
        }

    def log_test_start(self, test_name: str, test_args: list = None, extra_context: Dict[str, Any] = None):
        """Log when a test starts"""
        self.session_data["test_name"] = test_name
        self.session_data["test_args"] = test_args or []

        event = {
            "timestamp": datetime.now().isoformat(),
            "type": "test_start",
            "test": test_name,
            "args": test_args or [],
            "extra": extra_context or {}
        }
        self.session_data["events"].append(event)
        self._save_session()

        # Print to stderr so it's captured even if stdout is redirected
        print(f"[CRASH_LOG] Test started: {test_name} {' '.join(test_args or [])}", file=sys.stderr)

    def log_output(self, line: str):
        """Log output line (keeps last N lines for crash reconstruction)"""
        # Keep last 100 lines for context
        self.session_data["last_output_lines"].append({
            "timestamp": datetime.now().isoformat(),
            "text": line.strip()
        })
        if len(self.session_data["last_output_lines"]) > 100:
            self.session_data["last_output_lines"].pop(0)

    def log_crash(self, exit_code: int = None, signal_num: int = None, exception: Exception = None):
        """Log a crash with context"""
        crashed_at = datetime.now().isoformat()

        self.session_data["status"] = "crashed"
        self.session_data["ended_at"] = crashed_at
        self.session_data["crash_info"] = {
            "exit_code": exit_code,
            "signal": signal_num,
            "exception": str(exception) if exception else None
        }

        # Write crash file with last output for debugging
        with open(self.crash_file, "w") as f:
            f.write(f"CRASH LOG\n")
            f.write(f"{'='*80}\n")
            f.write(f"Session ID: {self.session_id}\n")
            f.write(f"Test: {self.session_data['test_name']}\n")
            f.write(f"Args: {self.session_data['test_args']}\n")
            f.write(f"Crashed at: {crashed_at}\n")
            f.write(f"Exit code: {exit_code}\n")
            f.write(f"Signal: {signal_num}\n")
            f.write(f"Exception: {exception}\n")
            f.write(f"\n{'='*80}\n")
            f.write(f"LAST OUTPUT (100 lines context):\n")
            f.write(f"{'='*80}\n")

            for entry in self.session_data["last_output_lines"]:
                f.write(f"[{entry['timestamp']}] {entry['text']}\n")

        self._save_session()

        # Print crash marker
        print(f"\n[CRASH_LOG] CRASH DETECTED", file=sys.stderr)
        print(f"[CRASH_LOG] Session: {self.session_id}", file=sys.stderr)
        print(f"[CRASH_LOG] Exit code: {exit_code}", file=sys.stderr)
        if signal_num:
            print(f"[CRASH_LOG] Signal: {signal_num}", file=sys.stderr)
        print(f"[CRASH_LOG] Crash log: {self.crash_file}", file=sys.stderr)

    def log_success(self):
        """Log successful test completion"""
        self.session_data["status"] = "success"
        self.session_data["ended_at"] = datetime.now().isoformat()
        self._save_session()

        print(f"[CRASH_LOG] Test completed successfully", file=sys.stderr)

    def _save_session(self):
        """Save session data to JSON"""
        with open(self.session_file, "w") as f:
            json.dump(self.session_data, f, indent=2)

    @staticmethod
    def list_recent_crashes(session_dir: str = "crash_logs", limit: int = 5):
        """List recent crashes for debugging"""
        crash_dir = Path(session_dir)
        if not crash_dir.exists():
            return []

        crashes = sorted(
            crash_dir.glob("crash_*.log"),
            key=os.path.getmtime,
            reverse=True
        )[:limit]

        return crashes

    @staticmethod
    def get_session_summary(session_dir: str = "crash_logs"):
        """Get summary of all recent sessions"""
        session_dir = Path(session_dir)
        if not session_dir.exists():
            return []

        sessions = []
        for session_file in sorted(session_dir.glob("session_*.json"), reverse=True)[:10]:
            try:
                with open(session_file) as f:
                    data = json.load(f)
                    sessions.append({
                        "id": data["session_id"],
                        "test": data["test_name"],
                        "status": data["status"],
                        "started": data["started_at"],
                        "crashed": data.get("crash_info", {}).get("exit_code") if data["status"] == "crashed" else None
                    })
            except:
                pass

        return sessions


def show_recent_crashes():
    """CLI tool to show recent crashes"""
    print("\n" + "="*80)
    print("RECENT TEST CRASHES")
    print("="*80 + "\n")

    sessions = CrashLogger.get_session_summary()

    if not sessions:
        print("No crash logs found.")
        return

    for session in sessions:
        status_str = "✓ SUCCESS" if session["status"] == "success" else "✗ CRASHED"
        print(f"{session['id']} | {session['test']:40} | {status_str}")
        print(f"  Started: {session['started']}")
        if session["crashed"] is not None:
            print(f"  Exit code: {session['crashed']}")
        print()

    # Show most recent crash details
    recent = CrashLogger.list_recent_crashes(limit=1)
    if recent:
        print("\n" + "="*80)
        print(f"MOST RECENT CRASH DETAILS")
        print("="*80 + "\n")
        with open(recent[0]) as f:
            print(f.read())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        show_recent_crashes()
    else:
        print("Usage: crash_logger.py show")
