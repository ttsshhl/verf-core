"""Subscription lifecycle: "expires soon" reminders and expiry.

Payment is a manual 30-day purchase (no auto-charge), so without this a plan
would stay "pro" forever after a single payment. Once an hour (idempotent):

  * 3 days before the end  -> one reminder email,
  * after the end          -> subscription marked expired; if the user has no
    other live subscription they drop to Free, containers get Free limits,
    projects beyond the Free quota are PAUSED (not deleted; they come back on
    the next payment), and an email explains it.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from app import config
from app.models import Subscription, SubscriptionStatus

_TIER = {"free": 0, "pro": 1, "business": 2}


def aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _best_live_other(db: Session, sub: Subscription, now: datetime) -> Subscription | None:
    others = db.query(Subscription).filter(
        Subscription.user_id == sub.user_id,
        Subscription.id != sub.id,
        Subscription.status == SubscriptionStatus.active,
    ).all()
    live = [o for o in others if o.expires_at and aware(o.expires_at) > now]
    return max(live, key=lambda o: (_TIER.get(o.plan, 0), aware(o.expires_at)), default=None)


def process_expirations(db: Session, background_tasks: BackgroundTasks, now: datetime | None = None) -> dict:
    from app import notifications
    from app.main import (
        _send_email_best_effort, _apply_plan_limits_best_effort, _pause_projects_best_effort,
    )

    now = now or datetime.now(timezone.utc)
    result = {"reminded": [], "expired": []}
    subs = db.query(Subscription).filter(
        Subscription.status == SubscriptionStatus.active, Subscription.expires_at.isnot(None)
    ).all()

    for sub in subs:
        exp = aware(sub.expires_at)
        user = sub.user
        if exp <= now:
            sub.status = SubscriptionStatus.expired
            live = _best_live_other(db, sub, now)
            if live:
                user.plan = live.plan          # renewed in advance — nothing changes for the user
                db.commit()
                continue
            old_plan = sub.plan
            user.plan = "free"
            db.commit()
            slugs = [p.slug for p in sorted(user.projects, key=lambda p: aware(p.created_at) or now)]
            keep = config.PLAN_PROJECT_LIMITS.get("free") or len(slugs)
            paused = slugs[keep:]
            background_tasks.add_task(_apply_plan_limits_best_effort, slugs, "free")
            if paused:
                background_tasks.add_task(_pause_projects_best_effort, paused)
            background_tasks.add_task(
                _send_email_best_effort, notifications.send_subscription_expired_email,
                user.email, old_plan, paused,
            )
            result["expired"].append(user.email)
        elif sub.reminded_at is None and exp - now <= timedelta(days=config.SUBSCRIPTION_REMINDER_DAYS):
            sub.reminded_at = now
            db.commit()
            live = _best_live_other(db, sub, now)
            if live and aware(live.expires_at) > exp:
                continue                        # already renewed — no need to nag
            background_tasks.add_task(
                _send_email_best_effort, notifications.send_subscription_expiring_email,
                user.email, sub.plan, exp,
            )
            result["reminded"].append(user.email)
    return result


def run_once() -> BackgroundTasks:
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        bt = BackgroundTasks()
        res = process_expirations(db, bt)
        if res["reminded"] or res["expired"]:
            print(f"[subscriptions] напоминаний: {len(res['reminded'])}, истекло: {len(res['expired'])}")
        return bt
    finally:
        db.close()


async def expiry_loop(interval: int) -> None:
    """Runs forever inside the API process. Never raises."""
    await asyncio.sleep(60)  # let the app settle after a restart
    while True:
        try:
            bt = await asyncio.to_thread(run_once)
            await bt()
        except Exception as exc:
            print(f"[subscriptions] ошибка проверки подписок: {exc}")
        await asyncio.sleep(interval)
