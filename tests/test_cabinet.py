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
            c.SessionLocal = TestingSessionLocal  # tests use this instead of app.db.SessionLocal (bound to a different DB)
            yield c
    finally:
        app.dependency_overrides.clear()


def _register(client, email="alice@example.com", password="correct-horse-battery"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- Registration / login ----------

def test_register_and_get_token(client):
    token = _register(client)
    assert len(token) > 20


def test_register_duplicate_email_rejected(client):
    _register(client, email="dupe@example.com")
    r = client.post("/auth/register", json={"email": "dupe@example.com", "password": "another-password"})
    assert r.status_code == 409


def test_register_short_password_rejected(client):
    r = client.post("/auth/register", json={"email": "x@example.com", "password": "short"})
    assert r.status_code == 422


def test_login_success(client):
    _register(client, email="bob@example.com", password="my-secure-password")
    r = client.post("/auth/login", json={"email": "bob@example.com", "password": "my-secure-password"})
    assert r.status_code == 200
    assert "access_token" in r.json()


def test_login_wrong_password_rejected(client):
    _register(client, email="carol@example.com", password="correct-password")
    r = client.post("/auth/login", json={"email": "carol@example.com", "password": "wrong-password"})
    assert r.status_code == 401


def test_login_unknown_email_rejected(client):
    r = client.post("/auth/login", json={"email": "ghost@example.com", "password": "whatever123"})
    assert r.status_code == 401


def test_me_requires_auth(client):
    r = client.get("/me")
    assert r.status_code == 401


def test_me_returns_current_user(client):
    token = _register(client, email="dave@example.com")
    r = client.get("/me", headers=_auth_headers(token))
    assert r.status_code == 200
    assert r.json()["email"] == "dave@example.com"
    assert r.json()["plan"] == "free"


def test_bogus_token_rejected(client):
    r = client.get("/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


# ---------- Personal-cabinet project ownership + plan limits ----------

def test_create_project_via_cabinet(client):
    token = _register(client)
    r = client.post(
        "/me/projects",
        headers=_auth_headers(token),
        json={"slug": "my-first-bot", "repo_url": "https://x.git", "branch": "main", "kind": "bot"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "my-first-bot"


def test_free_plan_limited_to_one_project(client):
    token = _register(client)
    ok = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "proj-one", "repo_url": "https://x.git", "branch": "main", "kind": "bot"},
    )
    assert ok.status_code == 200

    blocked = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "proj-two", "repo_url": "https://x.git", "branch": "main", "kind": "bot"},
    )
    assert blocked.status_code == 402  # Payment Required — exactly the right semantic here


def test_pro_plan_allows_five_projects(client, monkeypatch):
    from app.models import User

    token = _register(client, email="pro-user@example.com")
    # Simulate an active Pro subscription without going through real billing
    db = client.SessionLocal()
    user = db.query(User).filter_by(email="pro-user@example.com").first()
    user.plan = "pro"
    db.commit()
    db.close()

    for i in range(5):
        r = client.post(
            "/me/projects", headers=_auth_headers(token),
            json={"slug": f"pro-proj-{i}", "repo_url": "https://x.git", "branch": "main", "kind": "bot"},
        )
        assert r.status_code == 200, r.text

    sixth = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "pro-proj-6", "repo_url": "https://x.git", "branch": "main", "kind": "bot"},
    )
    assert sixth.status_code == 402


def test_users_only_see_own_projects(client):
    token_a = _register(client, email="a@example.com")
    token_b = _register(client, email="b@example.com")

    client.post("/me/projects", headers=_auth_headers(token_a),
                json={"slug": "a-proj", "repo_url": "https://x.git", "branch": "main", "kind": "bot"})
    client.post("/me/projects", headers=_auth_headers(token_b),
                json={"slug": "b-proj", "repo_url": "https://x.git", "branch": "main", "kind": "bot"})

    a_projects = client.get("/me/projects", headers=_auth_headers(token_a)).json()
    assert len(a_projects) == 1
    assert a_projects[0]["slug"] == "a-proj"


def test_cannot_delete_other_users_project(client):
    token_a = _register(client, email="owner@example.com")
    token_b = _register(client, email="attacker@example.com")

    client.post("/me/projects", headers=_auth_headers(token_a),
                json={"slug": "owners-bot", "repo_url": "https://x.git", "branch": "main", "kind": "bot"})

    r = client.delete("/me/projects/owners-bot", headers=_auth_headers(token_b))
    assert r.status_code == 404  # not found *for this user* — doesn't leak that it exists for someone else


def test_my_deployments_empty_for_fresh_project(client):
    token = _register(client, email="fresh@example.com")
    client.post("/me/projects", headers=_auth_headers(token),
                json={"slug": "fresh-proj", "repo_url": "https://x.git", "branch": "main", "kind": "bot"})
    r = client.get("/me/projects/fresh-proj/deployments", headers=_auth_headers(token))
    assert r.status_code == 200
    assert r.json() == []


def test_my_deployments_404_for_other_users_project(client):
    token_a = _register(client, email="dep-owner@example.com")
    token_b = _register(client, email="dep-attacker@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a),
                json={"slug": "dep-proj", "repo_url": "https://x.git", "branch": "main", "kind": "bot"})
    r = client.get("/me/projects/dep-proj/deployments", headers=_auth_headers(token_b))
    assert r.status_code == 404


# ---------- Billing ----------

def test_subscribe_creates_payment(client, monkeypatch):
    from app import billing
    monkeypatch.setattr(billing, "create_payment", lambda plan, sub_id: {
        "id": "yk_payment_123",
        "confirmation": {"confirmation_url": "https://yookassa.ru/pay/yk_payment_123"},
    })

    token = _register(client)
    r = client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"] == "pro"
    assert body["amount_rub"] == 990
    assert body["status"] == "pending"
    assert body["confirmation_url"] == "https://yookassa.ru/pay/yk_payment_123"


def test_subscribe_invalid_plan_rejected(client):
    token = _register(client)
    r = client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "ultra-mega"})
    assert r.status_code == 400


def test_subscribe_billing_error_cleans_up_pending_subscription(client, monkeypatch):
    from app import billing
    from app.models import Subscription

    def boom(plan, sub_id):
        raise billing.BillingError("ЮKassa недоступна")

    monkeypatch.setattr(billing, "create_payment", boom)

    token = _register(client, email="fail@example.com")
    r = client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro"})
    assert r.status_code == 502

    db = client.SessionLocal()
    assert db.query(Subscription).count() == 0  # cleaned up, not left dangling in "pending" forever
    db.close()


def test_webhook_activates_subscription_on_succeeded_status(client, monkeypatch):
    from app import billing
    from app.models import User, Subscription, SubscriptionStatus

    monkeypatch.setattr(billing, "create_payment", lambda plan, sub_id: {
        "id": "yk_pay_activate", "confirmation": {"confirmation_url": "https://yookassa.ru/pay/x"},
    })
    token = _register(client, email="activate@example.com")
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro"})

    # The webhook body itself claims success, but that's NOT what we trust —
    # we trust only what fetch_payment (our authenticated call to ЮKassa) returns.
    monkeypatch.setattr(billing, "fetch_payment", lambda payment_id: {"id": payment_id, "status": "succeeded"})

    r = client.post("/webhook/yookassa", json={"object": {"id": "yk_pay_activate", "status": "succeeded"}})
    assert r.status_code == 200

    db = client.SessionLocal()
    user = db.query(User).filter_by(email="activate@example.com").first()
    assert user.plan == "pro"
    sub = db.query(Subscription).filter_by(external_payment_id="yk_pay_activate").first()
    assert sub.status == SubscriptionStatus.active
    assert sub.expires_at is not None
    db.close()


def test_webhook_ignores_spoofed_body_that_disagrees_with_real_status(client, monkeypatch):
    """The core security property: even if the POSTed body claims 'succeeded',
    if ЮKassa's own API (fetch_payment) says otherwise, we do NOT activate.
    """
    from app import billing
    from app.models import User

    monkeypatch.setattr(billing, "create_payment", lambda plan, sub_id: {
        "id": "yk_pay_spoofed", "confirmation": {"confirmation_url": "https://yookassa.ru/pay/x"},
    })
    token = _register(client, email="victim@example.com")
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "business"})

    # Attacker POSTs a fake "succeeded" webhook, but the real API says "pending".
    monkeypatch.setattr(billing, "fetch_payment", lambda payment_id: {"id": payment_id, "status": "pending"})

    r = client.post("/webhook/yookassa", json={"object": {"id": "yk_pay_spoofed", "status": "succeeded"}})
    assert r.status_code == 200

    db = client.SessionLocal()
    user = db.query(User).filter_by(email="victim@example.com").first()
    assert user.plan == "free"  # NOT upgraded — the spoofed body was correctly ignored
    db.close()


def test_webhook_unknown_payment_id_ignored(client, monkeypatch):
    from app import billing
    monkeypatch.setattr(billing, "fetch_payment", lambda payment_id: {"id": payment_id, "status": "succeeded"})
    r = client.post("/webhook/yookassa", json={"object": {"id": "totally-unknown-id", "status": "succeeded"}})
    assert r.status_code == 200
    assert r.json() == {"ignored": "unknown payment_id"}


# ---------- Billing: Cryptomus ----------

def test_subscribe_with_cryptomus_creates_payment(client, monkeypatch):
    from app import billing
    monkeypatch.setattr(billing, "create_crypto_payment", lambda plan, sub_id: {
        "uuid": "crypto_pay_123", "url": "https://pay.cryptomus.com/pay/crypto_pay_123",
    })

    token = _register(client, email="crypto-buyer@example.com")
    r = client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro", "provider": "cryptomus"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmation_url"] == "https://pay.cryptomus.com/pay/crypto_pay_123"
    assert body["status"] == "pending"


def test_subscribe_invalid_provider_rejected(client):
    token = _register(client)
    r = client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro", "provider": "paypal"})
    assert r.status_code == 400


def test_cryptomus_webhook_activates_on_valid_signature_and_paid_status(client, monkeypatch):
    from app import billing
    from app.models import User, Subscription, SubscriptionStatus

    monkeypatch.setattr(billing, "create_crypto_payment", lambda plan, sub_id: {
        "uuid": "crypto_activate_1", "url": "https://pay.cryptomus.com/x",
    })
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "test-webhook-key")

    token = _register(client, email="crypto-activate@example.com")
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "business", "provider": "cryptomus"})

    payload = {"uuid": "crypto_activate_1", "status": "paid", "order_id": "whatever"}
    payload["sign"] = billing._cryptomus_sign(payload)

    r = client.post("/webhook/cryptomus", json=payload)
    assert r.status_code == 200

    db = client.SessionLocal()
    user = db.query(User).filter_by(email="crypto-activate@example.com").first()
    assert user.plan == "business"
    sub = db.query(Subscription).filter_by(external_payment_id="crypto_activate_1").first()
    assert sub.status == SubscriptionStatus.active
    assert sub.provider == "cryptomus"
    db.close()


def test_cryptomus_webhook_rejects_invalid_signature(client, monkeypatch):
    from app import billing
    from app.models import User

    monkeypatch.setattr(billing, "create_crypto_payment", lambda plan, sub_id: {
        "uuid": "crypto_forge_1", "url": "https://pay.cryptomus.com/x",
    })
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "real-key-the-attacker-does-not-have")

    token = _register(client, email="crypto-victim@example.com")
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro", "provider": "cryptomus"})

    # Attacker doesn't know the API key, so they can't produce a valid signature.
    forged = {"uuid": "crypto_forge_1", "status": "paid", "sign": "0" * 32}
    r = client.post("/webhook/cryptomus", json=forged)
    assert r.status_code == 401

    db = client.SessionLocal()
    user = db.query(User).filter_by(email="crypto-victim@example.com").first()
    assert user.plan == "free"  # not upgraded
    db.close()


def test_cryptomus_webhook_ignores_non_paid_status(client, monkeypatch):
    from app import billing
    from app.models import User

    monkeypatch.setattr(billing, "create_crypto_payment", lambda plan, sub_id: {
        "uuid": "crypto_pending_1", "url": "https://pay.cryptomus.com/x",
    })
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "test-key")

    token = _register(client, email="crypto-pending@example.com")
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro", "provider": "cryptomus"})

    payload = {"uuid": "crypto_pending_1", "status": "wrong_amount"}
    payload["sign"] = billing._cryptomus_sign(payload)
    r = client.post("/webhook/cryptomus", json=payload)
    assert r.status_code == 200

    db = client.SessionLocal()
    user = db.query(User).filter_by(email="crypto-pending@example.com").first()
    assert user.plan == "free"  # still not upgraded — only paid/paid_over activate
    db.close()
