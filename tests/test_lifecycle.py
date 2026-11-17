import pytest
from datetime import datetime, timedelta, timezone
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import builder, auth as auth_module
    from app.db import Base, get_db
    from app.main import app

    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(auth_module, "JWT_SECRET", "test-secret-for-e2e")

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.init_db", lambda: None)
    try:
        with TestClient(app) as c:
            c.SessionLocal = TestingSessionLocal  # tests use this instead of app.db.SessionLocal (bound to a different DB)
            yield c
    finally:
        app.dependency_overrides.clear()


def _register(client, email="alice@example.com", password="Correct-Horse1!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, email="alice@example.com", password="Correct-Horse1!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _db_sub(client, email, plan="pro", status="active", days_left=30, provider="yookassa"):
    from app.models import User, Subscription, SubscriptionStatus
    db = client.SessionLocal()
    user = db.query(User).filter_by(email=email).first()
    sub = Subscription(user_id=user.id, plan=plan, status=SubscriptionStatus(status), provider=provider,
                       amount_rub=490, activated_at=datetime.now(timezone.utc),
                       expires_at=datetime.now(timezone.utc) + timedelta(days=days_left))
    user.plan = plan if status == "active" else user.plan
    db.add(sub); db.commit(); sid = sub.id; db.close()
    return sid


def _run_expirations(client):
    import asyncio
    from app import subscriptions
    db = client.SessionLocal()
    bt = BackgroundTasks()
    res = subscriptions.process_expirations(db, bt)
    db.close()
    asyncio.run(bt())
    return res


@pytest.fixture
def outbox(monkeypatch):
    from app import notifications
    sent = []
    for name in ("send_subscription_expiring_email", "send_subscription_expired_email",
                 "send_payment_canceled_email", "send_password_reset_email"):
        monkeypatch.setattr(notifications, name, (lambda n: lambda *a, **k: sent.append((n, a)))(name))
    return sent


# ---------- expiry ----------

def test_reminder_sent_once_three_days_before_end(client, outbox):
    _register(client, email="soon@example.com")
    _db_sub(client, "soon@example.com", days_left=2)
    assert _run_expirations(client)["reminded"] == ["soon@example.com"]
    assert [n for n, _ in outbox] == ["send_subscription_expiring_email"]
    _run_expirations(client)  # idempotent: no second reminder
    assert len(outbox) == 1


def test_no_reminder_far_from_end(client, outbox):
    _register(client, email="far@example.com")
    _db_sub(client, "far@example.com", days_left=20)
    assert _run_expirations(client) == {"reminded": [], "expired": []} and outbox == []


def test_expiry_downgrades_to_free_with_limits_and_pauses_extra_projects(client, outbox, monkeypatch):
    from app import deployer
    calls = {"limits": [], "paused": []}
    monkeypatch.setattr(deployer, "update_limits", lambda slug, mem, cpu: calls["limits"].append((slug, mem)) or True)
    monkeypatch.setattr(deployer, "pause_container", lambda slug: calls["paused"].append(slug) or True)
    token = _register(client, email="lapse@example.com")
    h = _auth_headers(token)
    _db_sub(client, "lapse@example.com", plan="pro", days_left=-1)
    for slug in ("first-bot", "second-bot", "third-bot"):
        assert client.post("/me/projects", headers=h, json={"slug": slug, "repo_url": "https://x.git", "branch": "main", "kind": "bot"}).status_code == 200
    res = _run_expirations(client)
    assert res["expired"] == ["lapse@example.com"]
    assert client.get("/me", headers=h).json()["plan"] == "free"
    assert sorted(s for s, _ in calls["limits"]) == ["first-bot", "second-bot", "third-bot"]
    assert all(m == "256m" for _, m in calls["limits"])
    assert sorted(calls["paused"]) == ["second-bot", "third-bot"]   # oldest stays running
    name, args = outbox[-1]
    assert name == "send_subscription_expired_email" and args[0] == "lapse@example.com" and sorted(args[2]) == ["second-bot", "third-bot"]
    assert client.get("/billing/payments", headers=h).json()["payments"][0]["status"] == "expired"


def test_expiry_keeps_plan_if_renewed_in_advance(client, outbox):
    token = _register(client, email="renew@example.com")
    _db_sub(client, "renew@example.com", plan="pro", days_left=-1)
    _db_sub(client, "renew@example.com", plan="pro", days_left=25)
    _run_expirations(client)
    assert client.get("/me", headers=_auth_headers(token)).json()["plan"] == "pro"
    assert outbox == []


def test_early_renewal_adds_to_remaining_days(client, monkeypatch):
    from app import billing
    from app.models import Subscription
    monkeypatch.setattr(billing, "create_payment", lambda plan, sid, payment_method_type=None: {"id": "yk_ren", "confirmation": {"confirmation_url": "https://x"}})
    monkeypatch.setattr(billing, "fetch_payment", lambda pid: {"id": pid, "status": "succeeded"})
    token = _register(client, email="early@example.com"); h = _auth_headers(token)
    _db_sub(client, "early@example.com", plan="pro", days_left=10)
    client.post("/billing/subscribe", headers=h, json={"plan": "pro"})
    client.post("/billing/sync", headers=h)
    until = client.get("/billing/payments", headers=h).json()["active_until"]
    left = (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 39.5 < left < 40.5


# ---------- canceled payment email ----------

def test_canceled_payment_emails_customer_once(client, outbox, monkeypatch):
    from app import billing
    monkeypatch.setattr(billing, "create_payment", lambda plan, sid, payment_method_type=None: {"id": "yk_cancel", "confirmation": {"confirmation_url": "https://x"}})
    monkeypatch.setattr(billing, "fetch_payment", lambda pid: {"id": pid, "status": "canceled"})
    token = _register(client, email="nopay@example.com")
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro"})
    for _ in range(2):  # repeated webhook must not email twice
        client.post("/webhook/yookassa", json={"object": {"id": "yk_cancel"}})
    assert [(n, a[0]) for n, a in outbox] == [("send_payment_canceled_email", "nopay@example.com")]


# ---------- password reset ----------

def _reset_link_token(outbox):
    link = outbox[-1][1][1]
    assert link.endswith(link.split("?reset=")[1]) and "?reset=" in link
    return link.split("?reset=")[1]


def test_forgot_then_reset_logs_in_and_invalidates_link(client, outbox):
    _register(client, email="forgetful@example.com", password="Old-Password1!")
    assert client.post("/auth/forgot", json={"email": "forgetful@example.com"}).json() == {"ok": True}
    token = _reset_link_token(outbox)
    r = client.post("/auth/reset", json={"token": token, "password": "New-Password2!"})
    assert r.status_code == 200 and r.json()["access_token"]
    assert client.post("/auth/login", json={"email": "forgetful@example.com", "password": "New-Password2!"}).status_code == 200
    assert client.post("/auth/login", json={"email": "forgetful@example.com", "password": "Old-Password1!"}).status_code == 401
    again = client.post("/auth/reset", json={"token": token, "password": "Third-Password3!"})
    assert again.status_code == 400   # link already used


def test_forgot_does_not_reveal_unknown_addresses(client, outbox):
    r = client.post("/auth/forgot", json={"email": "ghost@example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True} and outbox == []


def test_forgot_is_rate_limited(client, outbox):
    from app import main
    main._RESET_REQUESTS.clear()
    _register(client, email="spam@example.com")
    for _ in range(6):
        client.post("/auth/forgot", json={"email": "spam@example.com"})
    assert len(outbox) == 3


def test_reset_rejects_weak_password_and_foreign_tokens(client, outbox):
    token = _register(client, email="weak@example.com")
    client.post("/auth/forgot", json={"email": "weak@example.com"})
    rt = _reset_link_token(outbox)
    assert client.post("/auth/reset", json={"token": rt, "password": "weakpass"}).status_code == 422
    assert client.post("/auth/reset", json={"token": token, "password": "Strong-Pass1!"}).status_code == 400  # a login token is not a reset token
    assert client.post("/auth/reset", json={"token": "garbage", "password": "Strong-Pass1!"}).status_code == 400


def test_reset_token_cannot_be_used_as_login_token(client, outbox):
    _register(client, email="swap@example.com")
    client.post("/auth/forgot", json={"email": "swap@example.com"})
    rt = _reset_link_token(outbox)
    assert client.get("/me", headers=_auth_headers(rt)).status_code == 401
