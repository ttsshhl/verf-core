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
            yield c
    finally:
        app.dependency_overrides.clear()


# ---------- app.alerts pure logic ----------

def test_send_admin_alert_raises_when_unconfigured(monkeypatch):
    from app import alerts
    monkeypatch.setattr(alerts, "TELEGRAM_ALERTS_BOT_TOKEN", "")
    monkeypatch.setattr(alerts, "TELEGRAM_ALERTS_CHAT_ID", "")
    with pytest.raises(alerts.AlertError):
        alerts.send_admin_alert("test")


def test_send_admin_alert_success(monkeypatch):
    from app import alerts
    monkeypatch.setattr(alerts, "TELEGRAM_ALERTS_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(alerts, "TELEGRAM_ALERTS_CHAT_ID", "999")

    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(alerts.requests, "post", fake_post)
    alerts.send_admin_alert("hello")
    assert "123:abc" in captured["url"]
    assert captured["json"]["chat_id"] == "999"
    assert captured["json"]["text"] == "hello"


def test_send_admin_alert_wraps_bad_status(monkeypatch):
    from app import alerts
    monkeypatch.setattr(alerts, "TELEGRAM_ALERTS_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(alerts, "TELEGRAM_ALERTS_CHAT_ID", "999")

    class FakeResponse:
        status_code = 400
        text = "bad chat id"

    monkeypatch.setattr(alerts.requests, "post", lambda *a, **kw: FakeResponse())
    with pytest.raises(alerts.AlertError):
        alerts.send_admin_alert("hello")


def test_send_admin_alert_wraps_network_error(monkeypatch):
    import requests
    from app import alerts
    monkeypatch.setattr(alerts, "TELEGRAM_ALERTS_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(alerts, "TELEGRAM_ALERTS_CHAT_ID", "999")

    def boom(*a, **kw):
        raise requests.RequestException("network down")

    monkeypatch.setattr(alerts.requests, "post", boom)
    with pytest.raises(alerts.AlertError):
        alerts.send_admin_alert("hello")


def test_new_user_registered_formats_message(monkeypatch):
    from app import alerts
    captured = {}
    monkeypatch.setattr(alerts, "send_admin_alert", lambda text: captured.update(text=text))
    alerts.new_user_registered("someone@example.com")
    assert "someone@example.com" in captured["text"]


# ---------- registration hook ----------

def test_register_sends_admin_alert(client, monkeypatch):
    from app import alerts

    captured = {}
    monkeypatch.setattr(alerts, "new_user_registered", lambda email: captured.update(email=email))

    r = client.post("/auth/register", json={"email": "newperson@example.com", "password": "Correct-Horse1!"})
    assert r.status_code == 200
    assert captured.get("email") == "newperson@example.com"


def test_register_succeeds_even_if_alert_sending_fails(client, monkeypatch):
    """Best-effort by design: a broken alerts bot must never break
    registration itself — same convention as the welcome email."""
    from app import alerts

    def boom(email):
        raise alerts.AlertError("Telegram is down")

    monkeypatch.setattr(alerts, "new_user_registered", boom)

    r = client.post("/auth/register", json={"email": "resilient2@example.com", "password": "Correct-Horse1!"})
    assert r.status_code == 200
    assert "access_token" in r.json()
