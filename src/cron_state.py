"""Missed-job catchup tracker for the scheduler.

Problem: APScheduler's `misfire_grace_time` only catches very recent misses
(seconds-to-minutes). If the laptop is off when a weekly or monthly job is
due, that job is **silently skipped forever** when the scheduler restarts.

Solution: persist the last successful run timestamp for each named job to
`data/cron_state.json`. On scheduler startup, compute the "most-recent
expected fire" for each job's schedule; if `last_run < expected`, we fire
the job once (catchup), then resume the normal schedule.

Coalesce policy: missed runs are coalesced — we never fire a job multiple
times in a row to "catch up the backlog". One catchup per missed schedule
cycle is enough (a monthly job runs once even if 3 months were missed).

Fresh-install policy: an empty state file is initialised to NOW for every
known job, so the first boot doesn't trigger every job at once.
"""
from __future__ import annotations

import json
import logging
import pytz
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import clock
from .config import settings

log = logging.getLogger(__name__)

STATE_FILE = settings.root / "data" / "cron_state.json"

# Every named job that participates in the catchup system. Adding a new
# scheduled job? Append it here and call `record_run(name)` on success.
KNOWN_JOBS: tuple[str, ...] = (
    "watchlist_refresh",
    "weekly_backtest",
    "monthly_optuna",
    "daily_blacklist",
    "self_review",
    "universe_refresh",
    "auto_budget",
    "weekly_autopilot",        # P2-1 DeepSeek autonomous manager (2026-06-26)
    "monthly_lever_recheck",   # monthly TP/SL drift revalidation
    "premarket_gap_sentinel",  # pre-open overnight gap check
    "open_gap_exit",           # at-open gap-risk execution
    "preopen_clock_check",     # daily 08:30 ET time sync + session confirmation
    "sandbox_diff",            # weekly sandbox↔backtest diff — was in main.py's
                               # catchup plan but never listed here, so on an
                               # install with no record it could not catch up
                               # (needs_catchup returns False for unknown names)
    "grid_sweep_weekly",       # parameter grid, ex-crontab (2026-07-28)
    "grid_sweep_daily",        # daily neighborhood walk, ex-crontab
)


# ---------- persistence ----------

def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("cron_state load failed (%s) — starting fresh", e)
        return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------- public state API ----------

def record_run(job_name: str) -> None:
    """Mark `job_name` as having successfully run NOW. Idempotent."""
    state = _load()
    state[job_name] = {"last_run": clock.ny_now().isoformat()}
    _save(state)


def last_run(job_name: str) -> Optional[datetime]:
    """Return last successful run as a NY-aware datetime, or None if unknown."""
    entry = _load().get(job_name)
    if not entry:
        return None
    try:
        ts = entry["last_run"]
        # `ny_now()` writes tz-aware ISO; fromisoformat needs the tz suffix
        # in 3.11+. Strip 'Z' if a future change introduces it.
        return datetime.fromisoformat(ts.replace("Z", ""))
    except (KeyError, ValueError):
        return None


def initialize_if_empty() -> bool:
    """First-boot guard: seed every KNOWN_JOBS with last_run=now so we don't
    fire all of them at first start. Returns True if it actually wrote."""
    state = _load()
    if state:
        return False
    now_iso = clock.ny_now().isoformat()
    for job in KNOWN_JOBS:
        state[job] = {"last_run": now_iso}
    _save(state)
    log.info("cron_state: first boot — initialised %d jobs to %s",
             len(KNOWN_JOBS), now_iso)
    return True


# ---------- "expected last fire" calculators ----------

def expected_last_fire_daily(hour: int, minute: int,
                              weekdays_only: bool = True) -> datetime:
    """Most recent past fire time for a daily cron at HH:MM.

    If `weekdays_only`, skip Sat/Sun back to the previous weekday.
    """
    now = clock.ny_now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=1)
    if weekdays_only:
        # weekday(): Mon=0 ... Sun=6
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
    return candidate


def expected_last_fire_weekly(weekday: int, hour: int, minute: int,
                              tz=None) -> datetime:
    """Most recent past fire for a weekly cron on (weekday) at HH:MM.

    `weekday`: 0=Mon ... 6=Sun.
    `tz`: optional tzinfo the (weekday, HH:MM) is expressed in — e.g. the
    Monday-evening KL jobs pass Asia/Kuala_Lumpur so the expected fire matches
    the cron's own timezone-pinned schedule. Default None = NY (legacy).
    The returned datetime is tz-aware either way; needs_catchup compares
    aware-vs-aware correctly across zones.
    """
    now = clock.ny_now()
    if tz is not None:
        now = now.astimezone(tz)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_back = (now.weekday() - weekday) % 7
    # If today IS the cron day but the candidate time is still in the future,
    # the most recent fire was a week ago.
    if days_back == 0 and candidate > now:
        days_back = 7
    return candidate - timedelta(days=days_back)


KL = pytz.timezone("Asia/Kuala_Lumpur")

# ── The ONE definition of when each weekly job is due ────────────────────────
# (weekday, hour, minute) in KL — timezone-pinned so US DST never shifts the
# wall-clock time. Every caller that asks "was this job missed?" MUST derive
# from here via expected_last_fire().
#
# 2026-07-27: added because two callers disagreed about `self_review`.
# main.py's catchup used Mon 20:15 KL while web/server.py's boot catchup used
# Sun 23:00 ET (~9h apart, different key for the same job), so a restart could
# satisfy one and not the other: on 2026-07-27 the web boot fired the review at
# 22:30 and main.py's catchup fired it AGAIN at 22:37. Both paths run
# optimizer_ai.propose_from_review, which auto-applies params — exactly the
# "double autopilot param applies" hazard _run_catchup_on_startup warns about,
# but across processes where no in-process guard can see it.
#
# ⚠ These MUST stay in sync with the run_loop cron schedule in main.py. If a
# job's cron time changes, change it HERE.
WEEKLY_SCHEDULE: dict[str, tuple[int, int, int]] = {
    "weekly_autopilot": (0, 20, 0),    # Mon 20:00 KL
    "universe_refresh": (0, 20, 5),    # Mon 20:05 KL
    "weekly_backtest":  (0, 20, 10),   # Mon 20:10 KL
    "self_review":      (0, 20, 15),   # Mon 20:15 KL
    "sandbox_diff":     (0, 20, 25),   # Mon 20:25 KL
    # 2026-07-28: the grid sweep, moved off the user's crontab and into the
    # scheduler (see main._grid_sweep_job). Mon 07:00 KL == 19:00 ET Sunday —
    # market closed, which the sweep REQUIRES because it injects grid params
    # into live db-state while it runs. Same slot the crontab used.
    "grid_sweep_weekly": (0, 7, 0),    # Mon 07:00 KL
}

# Daily jobs that follow the same "one definition" rule. (weekday range is
# expressed the APScheduler way; expected_last_fire_daily handles weekdays_only.)
# grid_sweep_daily ran Tue-Sat 09:00 KL under cron == 21:00 ET Mon-Fri, again
# market-closed. Kept daily-weekday here; the ±1 day vs Tue-Sat is immaterial
# because the sweep no-ops when the market is open and records its own run.
DAILY_KL_SCHEDULE: dict[str, tuple[int, int]] = {
    "grid_sweep_daily": (9, 0),        # 09:00 KL, weekdays
}


def expected_last_fire_daily_kl(job: str) -> datetime:
    """Most recent past fire for a known daily KL job."""
    hour, minute = DAILY_KL_SCHEDULE[job]
    now = clock.ny_now().astimezone(KL)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=1)
    return candidate


def expected_last_fire(job: str) -> datetime:
    """Most recent past fire for a known weekly job — the single source of
    truth shared by every catchup caller. Raises KeyError on an unknown job
    (better than silently inventing a schedule the real cron doesn't use)."""
    weekday, hour, minute = WEEKLY_SCHEDULE[job]
    return expected_last_fire_weekly(weekday, hour, minute, tz=KL)


def expected_last_fire_monthly(day: int, hour: int, minute: int) -> datetime:
    """Most recent past fire for a monthly cron on the N-th day at HH:MM."""
    now = clock.ny_now()
    try:
        candidate = now.replace(day=day, hour=hour, minute=minute,
                                second=0, microsecond=0)
    except ValueError:
        # Month has fewer days than `day` (e.g. Feb 30) — fall back to the
        # 1st of the next month, then step back.
        candidate = now.replace(day=1, hour=hour, minute=minute,
                                second=0, microsecond=0)
    if candidate > now:
        # Go back to the previous month.
        if candidate.month == 1:
            candidate = candidate.replace(year=candidate.year - 1, month=12)
        else:
            candidate = candidate.replace(month=candidate.month - 1)
    return candidate


# ---------- catchup decision ----------

def needs_catchup(job_name: str, expected_fire: datetime) -> bool:
    """True iff the job's last_run is BEFORE the most recent expected fire."""
    lr = last_run(job_name)
    if lr is None:
        # No record. A genuine first boot is handled earlier by
        # initialize_if_empty() (which seeds every KNOWN_JOBS and short-circuits
        # catchup), so reaching here with no record means this KNOWN job was
        # added to an EXISTING install (e.g. self_review) or never succeeded —
        # in both cases it's overdue and should catch up once. An unknown job
        # name stays conservative (no catchup).
        return job_name in KNOWN_JOBS
    # Strip tz info for naive comparison if datetimes differ in tz handling.
    if lr.tzinfo is None and expected_fire.tzinfo is not None:
        expected_fire = expected_fire.replace(tzinfo=None)
    elif lr.tzinfo is not None and expected_fire.tzinfo is None:
        lr = lr.replace(tzinfo=None)
    return lr < expected_fire
