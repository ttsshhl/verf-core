"""Free-plan trial period.

Free is time-limited (app.config.FREE_TRIAL_DAYS from registration) —
Pro/Business have no such limit since they're already paid. Kept as its
own tiny module so the date-math is easy to test in isolation and reused
identically everywhere it's enforced (creating a project, git-push deploy,
ZIP-upload deploy, the daily sweep that pauses expired projects).
"""
from datetime import datetime, timezone

from app.config import FREE_TRIAL_DAYS


def _aware(dt: datetime) -> datetime:
    """SQLite has no native timezone-aware datetime type, so a timestamp
    written as UTC-aware (see app.models.utcnow) commonly comes back naive
    when read from the DB. Treat a naive value as UTC — matches how it was
    written — so subtracting from an aware "now" never raises.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def trial_days_left(plan: str, created_at: datetime) -> int | None:
    """Whole days left in the trial, or None for a paid plan (no limit).
    Can go negative once expired — callers that only care about the
    yes/no question use is_trial_expired() instead.
    """
    if plan != "free":
        return None
    elapsed = datetime.now(timezone.utc) - _aware(created_at)
    return FREE_TRIAL_DAYS - elapsed.days


def is_trial_expired(plan: str, created_at: datetime) -> bool:
    days_left = trial_days_left(plan, created_at)
    return days_left is not None and days_left < 0
