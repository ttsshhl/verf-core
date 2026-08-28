import pytest
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
            c.SessionLocal = TestingSessionLocal
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- app.notifications / app.email pure logic ----------

def test_send_email_raises_when_smtp_not_configured(monkeypatch):
    from app import notifications as email_mod
    monkeypatch.setattr(email_mod, "SMTP_HOST", "smtp.yandex.ru")
    monkeypatch.setattr(email_mod, "SMTP_USER", "")
    monkeypatch.setattr(email_mod, "SMTP_PASSWORD", "")
    with pytest.raises(email_mod.EmailError):
        email_mod.send_email("someone@example.com", "Subject", "<p>hi</p>")


def test_send_email_wraps_smtp_exceptions(monkeypatch):
    import smtplib
    from app import notifications as email_mod
    monkeypatch.setattr(email_mod, "SMTP_HOST", "smtp.yandex.ru")
    monkeypatch.setattr(email_mod, "SMTP_USER", "bot@yandex.ru")
    monkeypatch.setattr(email_mod, "SMTP_PASSWORD", "app-password")

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): raise smtplib.SMTPAuthenticationError(535, b"bad creds")

    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", FakeSMTP)
    with pytest.raises(email_mod.EmailError):
        email_mod.send_email("someone@example.com", "Subject", "<p>hi</p>")


def test_send_email_success_calls_sendmail(monkeypatch):
    from app import notifications as email_mod
    monkeypatch.setattr(email_mod, "SMTP_HOST", "smtp.yandex.ru")
    monkeypatch.setattr(email_mod, "SMTP_USER", "bot@yandex.ru")
    monkeypatch.setattr(email_mod, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(email_mod, "SMTP_FROM_EMAIL", "bot@yandex.ru")

    calls = {}

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, user, pw): calls["login"] = (user, pw)
        def sendmail(self, from_addr, to_addrs, msg): calls["sendmail"] = (from_addr, to_addrs)

    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", FakeSMTP)
    email_mod.send_email("recipient@example.com", "Hello", "<p>hi</p>")
    assert calls["login"] == ("bot@yandex.ru", "app-password")
    assert calls["sendmail"][1] == ["recipient@example.com"]


def test_welcome_email_uses_send_email(monkeypatch):
    from app import notifications as email_mod
    captured = {}
    monkeypatch.setattr(email_mod, "send_email", lambda to, subject, html: captured.update(to=to, subject=subject))
    email_mod.send_welcome_email("new-user@example.com")
    assert captured["to"] == "new-user@example.com"
    assert "VERF" in captured["subject"]


def test_payment_confirmation_email_includes_plan_and_amount(monkeypatch):
    from app import notifications as email_mod
    captured = {}
    monkeypatch.setattr(email_mod, "send_email", lambda to, subject, html: captured.update(to=to, subject=subject, html=html))
    email_mod.send_payment_confirmation_email("payer@example.com", "pro", 490)
    assert captured["to"] == "payer@example.com"
    assert "Pro" in captured["subject"]
    assert "490" in captured["html"]


# ---------- end-to-end wiring: registration ----------

def test_register_sends_welcome_email(client, monkeypatch):
    from app import notifications

    captured = {}
    monkeypatch.setattr(notifications, "send_welcome_email", lambda to: captured.update(to=to))

    r = client.post("/auth/register", json={"email": "welcome-me@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 200
    assert captured.get("to") == "welcome-me@example.com"


def test_register_succeeds_even_if_email_sending_fails(client, monkeypatch):
    """The whole point of best-effort: a broken SMTP config must never break
    registration itself."""
    from app import notifications

    def boom(to):
        raise notifications.EmailError("SMTP is down")

    monkeypatch.setattr(notifications, "send_welcome_email", boom)

    r = client.post("/auth/register", json={"email": "resilient@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 200
    assert "access_token" in r.json()


# ---------- end-to-end wiring: payment activation ----------

def test_yookassa_webhook_sends_payment_confirmation_email(client, monkeypatch):
    from app import billing, notifications

    monkeypatch.setattr(billing, "create_payment", lambda plan, sub_id, payment_method_type=None: {
        "id": "yk_email_test", "confirmation": {"confirmation_url": "https://yookassa.ru/pay/x"},
    })

    r = client.post("/auth/register", json={"email": "payer-email@example.com", "password": "correct-horse-battery"})
    token = r.json()["access_token"]
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro"})

    captured = {}
    monkeypatch.setattr(
        notifications, "send_payment_confirmation_email",
        lambda to, plan, amount: captured.update(to=to, plan=plan, amount=amount),
    )
    monkeypatch.setattr(billing, "fetch_payment", lambda payment_id: {"id": payment_id, "status": "succeeded"})

    resp = client.post("/webhook/yookassa", json={"object": {"id": "yk_email_test", "status": "succeeded"}})
    assert resp.status_code == 200
    assert captured.get("to") == "payer-email@example.com"
    assert captured.get("plan") == "pro"
    assert captured.get("amount") == 490


def test_payment_activation_succeeds_even_if_email_sending_fails(client, monkeypatch):
    from app import billing, notifications

    monkeypatch.setattr(billing, "create_payment", lambda plan, sub_id, payment_method_type=None: {
        "id": "yk_email_fail_test", "confirmation": {"confirmation_url": "https://yookassa.ru/pay/x"},
    })

    r = client.post("/auth/register", json={"email": "resilient-payer@example.com", "password": "correct-horse-battery"})
    token = r.json()["access_token"]
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "business"})

    def boom(to, plan, amount):
        raise notifications.EmailError("SMTP is down")

    monkeypatch.setattr(notifications, "send_payment_confirmation_email", boom)
    monkeypatch.setattr(billing, "fetch_payment", lambda payment_id: {"id": payment_id, "status": "succeeded"})

    resp = client.post("/webhook/yookassa", json={"object": {"id": "yk_email_fail_test", "status": "succeeded"}})
    assert resp.status_code == 200  # webhook still succeeds

    from app.models import User
    db = client.SessionLocal()
    user = db.query(User).filter_by(email="resilient-payer@example.com").first()
    assert user.plan == "business"  # subscription still activated despite email failure
    db.close()
