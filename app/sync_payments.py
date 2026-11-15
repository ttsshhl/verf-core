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
