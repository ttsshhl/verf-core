from datetime import datetime, timedelta, timezone

from app.trial import trial_days_left, is_trial_expired


def test_paid_plans_have_no_trial_limit():
    now = datetime.now(timezone.utc)
    assert trial_days_left("pro", now) is None
    assert trial_days_left("business", now) is None
    assert is_trial_expired("pro", now - timedelta(days=999)) is False
    assert is_trial_expired("business", now - timedelta(days=999)) is False


def test_fresh_free_account_has_full_trial_remaining():
    now = datetime.now(timezone.utc)
    assert trial_days_left("free", now) == 7
    assert is_trial_expired("free", now) is False


def test_free_account_mid_trial():
    created = datetime.now(timezone.utc) - timedelta(days=3)
    assert trial_days_left("free", created) == 4
    assert is_trial_expired("free", created) is False


def test_free_account_exactly_at_boundary_not_yet_expired():
    """6 days elapsed, trial is 7 days — still has (a fraction of) a day left."""
    created = datetime.now(timezone.utc) - timedelta(days=6, hours=23)
    assert is_trial_expired("free", created) is False


def test_free_account_past_trial_is_expired():
    created = datetime.now(timezone.utc) - timedelta(days=8)
    assert is_trial_expired("free", created) is True
    assert trial_days_left("free", created) == -1


def test_handles_naive_datetime_from_sqlite():
    """SQLite commonly returns a naive datetime even though it was written
    as UTC-aware — this must not raise on the aware/naive subtraction."""
    naive_created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    assert trial_days_left("free", naive_created) == 6
    assert is_trial_expired("free", naive_created) is False
