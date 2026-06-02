#!/usr/bin/env python3
"""
Lightweight Background Scheduler for GitHub Trending Sticky Note.

Runs continuously in the background, periodically refreshing the sticky note.
Uses concepts from the task-queue pattern adapted to single-machine desktop:
  - Job state tracking (pending → running → succeeded/failed)
  - Idempotent execution (safe to re-run anytime)
  - Graceful shutdown on system signals
  - Dead-letter queue for persistent failures

No dependencies beyond Python stdlib. Start via startup shortcut.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR = Path(__file__).resolve().parent
FETCH_SCRIPT = SCRIPT_DIR / "fetch_trending.py"
STATE_FILE = SCRIPT_DIR / "scheduler_state.json"
LOG_FILE = SCRIPT_DIR / "scheduler.log"

# Run update every N hours (if always-on) plus at 9:00 AM sharp
UPDATE_INTERVAL_HOURS: int = 6
MORNING_UPDATE_HOUR: int = 9
MORNING_UPDATE_MINUTE: int = 0

# How often to wake up and check "is it time?"
POLL_INTERVAL_SECONDS: int = 300  # 5 minutes


# ═══════════════════════════════════════════════════════════════════
# Domain Types
# ═══════════════════════════════════════════════════════════════════

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class JobRecord:
    """Single job execution record."""
    run_id: int
    status: JobStatus
    started_at: str
    completed_at: str = ""
    exit_code: int = -1
    error: str = ""
    repos_count: int = 0


@dataclass
class SchedulerState:
    """Persistent scheduler state (idempotent — safe to re-load)."""
    total_runs: int = 0
    success_runs: int = 0
    failure_runs: int = 0
    consecutive_failures: int = 0
    last_success_at: str = ""
    last_failure_at: str = ""
    last_failure_error: str = ""
    recent_runs: list[JobRecord] = field(default_factory=list)

    MAX_RECENT: int = 20

    def add_run(self, record: JobRecord) -> None:
        """Record a job execution, trimming history to MAX_RECENT."""
        self.total_runs += 1
        self.recent_runs.append(record)
        if len(self.recent_runs) > self.MAX_RECENT:
            self.recent_runs = self.recent_runs[-self.MAX_RECENT:]

        if record.status == JobStatus.SUCCEEDED:
            self.success_runs += 1
            self.consecutive_failures = 0
            self.last_success_at = record.completed_at
        else:
            self.failure_runs += 1
            self.consecutive_failures += 1
            self.last_failure_at = record.completed_at
            self.last_failure_error = record.error


# ═══════════════════════════════════════════════════════════════════
# State Persistence
# ═══════════════════════════════════════════════════════════════════

def load_state() -> SchedulerState:
    """Load scheduler state from disk, returning fresh state on failure."""
    if not STATE_FILE.exists():
        return SchedulerState()

    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        recent = [JobRecord(**r) for r in raw.get("recent_runs", [])]
        return SchedulerState(
            total_runs=raw.get("total_runs", 0),
            success_runs=raw.get("success_runs", 0),
            failure_runs=raw.get("failure_runs", 0),
            consecutive_failures=raw.get("consecutive_failures", 0),
            last_success_at=raw.get("last_success_at", ""),
            last_failure_at=raw.get("last_failure_at", ""),
            last_failure_error=raw.get("last_failure_error", ""),
            recent_runs=recent,
        )
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        _log(f"[WARN] State file corrupt, starting fresh: {e}")
        return SchedulerState()


def save_state(state: SchedulerState) -> None:
    """Persist scheduler state atomically."""
    raw: dict[str, Any] = {
        "total_runs": state.total_runs,
        "success_runs": state.success_runs,
        "failure_runs": state.failure_runs,
        "consecutive_failures": state.consecutive_failures,
        "last_success_at": state.last_success_at,
        "last_failure_at": state.last_failure_at,
        "last_failure_error": state.last_failure_error,
        "recent_runs": [
            {
                "run_id": r.run_id,
                "status": r.status.value,
                "started_at": r.started_at,
                "completed_at": r.completed_at,
                "exit_code": r.exit_code,
                "error": r.error,
                "repos_count": r.repos_count,
            }
            for r in state.recent_runs
        ],
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as e:
        _log(f"[WARN] Could not save state: {e}")


# ═══════════════════════════════════════════════════════════════════
# Logging (file + stdout)
# ═══════════════════════════════════════════════════════════════════

def _log(message: str) -> None:
    """Write timestamped message to log file and stdout."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # Log failure is non-fatal


# ═══════════════════════════════════════════════════════════════════
# Job Runner (idempotent)
# ═══════════════════════════════════════════════════════════════════

def run_fetch_job(run_id: int) -> JobRecord:
    """Execute fetch_trending.py as a subprocess.

    Idempotent: safe to run any number of times — each invocation
    independently fetches data and regenerates the HTML file.

    Returns:
        JobRecord with status and details.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        run_id=run_id,
        status=JobStatus.RUNNING,
        started_at=started_at,
    )

    try:
        result = subprocess.run(
            [sys.executable, str(FETCH_SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,  # 2 minutes max for the entire fetch
            cwd=str(SCRIPT_DIR),
        )
        record.completed_at = datetime.now(timezone.utc).isoformat()
        record.exit_code = result.returncode

        if result.returncode == 0:
            record.status = JobStatus.SUCCEEDED
        else:
            record.status = JobStatus.FAILED
            record.error = result.stderr.strip()[-300:] or result.stdout.strip()[-300:]

        # Extract repo count from stdout for observability
        for line in result.stdout.splitlines():
            if "repos displayed" in line:
                try:
                    record.repos_count = int(line.split()[0])
                except (ValueError, IndexError):
                    pass

    except subprocess.TimeoutExpired:
        record.completed_at = datetime.now(timezone.utc).isoformat()
        record.status = JobStatus.FAILED
        record.error = "Fetch script timed out (>120s)"
    except OSError as e:
        record.completed_at = datetime.now(timezone.utc).isoformat()
        record.status = JobStatus.FAILED
        record.error = f"OS error: {e}"

    return record


# ═══════════════════════════════════════════════════════════════════
# Schedule Logic
# ═══════════════════════════════════════════════════════════════════

def _seconds_until_morning() -> float:
    """Seconds until next MORNING_UPDATE_HOUR:MINUTE."""
    now = datetime.now()
    target = now.replace(
        hour=MORNING_UPDATE_HOUR,
        minute=MORNING_UPDATE_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= target:
        # Already past today's time → tomorrow
        from datetime import timedelta
        target += timedelta(days=1)
    return (target - now).total_seconds()


def should_update(state: SchedulerState) -> tuple[bool, str]:
    """Check if it's time to run an update.

    Returns:
        (should_run, reason) tuple.
    """
    # Case 1: First run ever
    if state.total_runs == 0:
        return True, "initial run"

    # Case 2: It's been longer than UPDATE_INTERVAL_HOURS since last run
    if state.recent_runs:
        last_run = state.recent_runs[-1]
        last_time = datetime.fromisoformat(last_run.started_at)
        hours_since = (datetime.now(timezone.utc) - last_time.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if hours_since >= UPDATE_INTERVAL_HOURS:
            return True, f"{hours_since:.1f}h since last run (threshold: {UPDATE_INTERVAL_HOURS}h)"

    # Case 3: Consecutive failures → retry sooner
    if state.consecutive_failures >= 3 and state.consecutive_failures < 10:
        last_run = state.recent_runs[-1]
        last_time = datetime.fromisoformat(last_run.started_at)
        minutes_since = (datetime.now(timezone.utc) - last_time.replace(tzinfo=timezone.utc)).total_seconds() / 60
        if minutes_since >= 30:
            return True, f"retry after {state.consecutive_failures} failures ({minutes_since:.0f}m ago)"

    return False, "not yet"


# ═══════════════════════════════════════════════════════════════════
# Main Loop
# ═══════════════════════════════════════════════════════════════════

_shutdown_requested: bool = False


def _handle_signal(signum: int, frame: Any) -> None:
    """Graceful shutdown on SIGTERM / SIGINT."""
    global _shutdown_requested
    _shutdown_requested = True
    _log(f"Received signal {signum}. Shutting down gracefully...")


def main() -> None:
    """Run the scheduler loop until terminated."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = load_state()
    _log("=" * 50)
    _log("Scheduler started")
    _log(f"State: {state.total_runs} total runs, {state.success_runs} ok, {state.failure_runs} failed")
    _log(f"Update interval: {UPDATE_INTERVAL_HOURS}h, poll: {POLL_INTERVAL_SECONDS}s")
    _log(f"Morning update: {MORNING_UPDATE_HOUR:02d}:{MORNING_UPDATE_MINUTE:02d}")

    # Run immediately on startup
    run_id = state.total_runs + 1
    _log(f"--- Run #{run_id} (startup) ---")
    record = run_fetch_job(run_id)
    state.add_run(record)
    save_state(state)
    _log(f"Result: {record.status.value} | repos={record.repos_count} | exit={record.exit_code}")

    while not _shutdown_requested:
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
        except InterruptedError:
            continue

        do_run, reason = should_update(state)
        if do_run:
            run_id = state.total_runs + 1
            _log(f"--- Run #{run_id} ({reason}) ---")
            record = run_fetch_job(run_id)
            state.add_run(record)
            save_state(state)
            _log(f"Result: {record.status.value} | repos={record.repos_count} | exit={record.exit_code}")
        # else: just poll

    _log(f"Scheduler stopped. Total runs: {state.total_runs}")
    save_state(state)


if __name__ == "__main__":
    main()
