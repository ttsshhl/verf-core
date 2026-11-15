"""Reconcile pending ЮKassa subscriptions with ЮKassa's own API.

The normal path is ЮKassa's HTTP notification to /webhook/yookassa. But if that
notification never arrives (webhook URL not set in the ЮKassa dashboard, server
was down or moved), a paid subscription would stay "pending" forever. This asks
ЮKassa directly — the authoritative source — so the plan activates anyway.

Run for everyone:  docker compose exec verf-core python -m app.sync_payments
"""
import asyncio

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from app import billing
from app.models import Subscription, SubscriptionStatus


def sync_pending(db: Session, background_tasks: BackgroundTasks, user_id: str | None = None) -> int:
    """Returns how many subscriptions were activated."""
    from app.main import _activate_subscription

    q = db.query(Subscription).filter_by(status=SubscriptionStatus.pending, provider="yookassa")
    if user_id:
        q = q.filter_by(user_id=user_id)
    activated = 0
    for sub in q.order_by(Subscription.created_at.desc()).limit(20).all():
        if not sub.external_payment_id:
            continue
        try:
            payment = billing.fetch_payment(sub.external_payment_id)
        except billing.BillingError:
            continue
        if payment.get("status") == "succeeded":
            _activate_subscription(db, sub, background_tasks)
            activated += 1
        elif payment.get("status") == "canceled":
            sub.status = SubscriptionStatus.canceled
            db.commit()
    return activated


def pending_count(db: Session, user_id: str) -> int:
    return db.query(Subscription).filter_by(
        user_id=user_id, status=SubscriptionStatus.pending, provider="yookassa"
    ).count()


def sync_once() -> BackgroundTasks:
    """One full reconciliation pass with its own DB session. Returns the
    BackgroundTasks holding follow-ups (emails, limits) for the caller to run."""
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        bt = BackgroundTasks()
        n = sync_pending(db, bt)
        if n:
            print(f"[sync] активировано подписок: {n}")
        return bt
    finally:
        db.close()


async def sync_loop(interval: int) -> None:
    """Runs forever inside the API process: every `interval` seconds asks
    ЮKassa about pending payments. Never raises — a failed pass just waits."""
    while True:
        await asyncio.sleep(interval)
        try:
            bt = await asyncio.to_thread(sync_once)
            await bt()
        except Exception as exc:
            print(f"[sync] ошибка фоновой сверки платежей: {exc}")


def main() -> None:
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        bt = BackgroundTasks()
        n = sync_pending(db, bt)
        asyncio.run(bt())
        print(f"Активировано подписок: {n}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
