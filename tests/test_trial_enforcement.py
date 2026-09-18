import hashlib
import hmac
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fake_upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "index.html").write_text("<h1>hi</h1>")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


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


def _register(client, email, password="Correct-Horse1!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _age_account(client, email: str, days: int):
    """Backdates a user's created_at, simulating an account whose trial
    started `days` ago — the only realistic way to test expiry without
    literally waiting a week."""
    from app.models import User
    db = client.SessionLocal()
    user = db.query(User).filter_by(email=email).first()
    user.created_at = datetime.now(timezone.utc) - timedelta(days=days)
    db.commit()
    db.close()


# ---------- /me shows trial status ----------

def test_me_shows_full_trial_for_fresh_account(client):
    token = _register(client, "fresh@example.com")
    r = client.get("/me", headers=_auth_headers(token))
    assert r.json()["trial_days_left"] == 7


def test_me_shows_none_for_paid_plan(client, monkeypatch):
    from app.models import User
    token = _register(client, "paiduser@example.com")
    db = client.SessionLocal()
    db.query(User).filter_by(email="paiduser@example.com").update({"plan": "pro"})
    db.commit()
    db.close()

    r = client.get("/me", headers=_auth_headers(token))
    assert r.json()["trial_days_left"] is None


# ---------- creating a project is blocked once trial expires ----------

def test_create_project_blocked_after_trial_expires(client):
    token = _register(client, "expired-create@example.com")
    _age_account(client, "expired-create@example.com", days=8)

    r = client.post("/me/projects", headers=_auth_headers(token), json={"slug": "too-late", "kind": "site"})
    assert r.status_code == 402
    assert "Пробный период" in r.json()["detail"]


def test_create_project_allowed_within_trial(client):
    token = _register(client, "within-trial@example.com")
    _age_account(client, "within-trial@example.com", days=3)

    r = client.post("/me/projects", headers=_auth_headers(token), json={"slug": "still-fine", "kind": "site"})
    assert r.status_code == 200


# ---------- ZIP-upload deploy is blocked once trial expires ----------

def test_zip_deploy_blocked_after_trial_expires(client, tmp_path):
    import zipfile
    token = _register(client, "expired-zip@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "zip-proj", "kind": "site"})
    _age_account(client, "expired-zip@example.com", days=10)

    zip_path = tmp_path / "site.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<h1>hi</h1>")

    with open(zip_path, "rb") as f:
        r = client.post(
            "/me/projects/zip-proj/deploy", headers=_auth_headers(token),
            files={"archive": ("site.zip", f, "application/zip")},
        )
    assert r.status_code == 402


# ---------- git-push webhook deploy is blocked once trial expires ----------

def test_webhook_deploy_blocked_after_trial_expires(client, fake_upstream):
    token = _register(client, "expired-webhook@example.com")
    proj = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "webhook-proj", "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()
    _age_account(client, "expired-webhook@example.com", days=9)

    body = json.dumps({"ref": "refs/heads/main", "after": "x"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    r = client.post(
        "/webhook/github/webhook-proj", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert r.status_code == 402


def test_webhook_ping_still_works_after_trial_expires(client, fake_upstream):
    """A ping is just GitHub's connectivity check at webhook-creation
    time, not an actual deploy attempt — must never be blocked."""
    token = _register(client, "expired-ping@example.com")
    proj = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "ping-proj", "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()
    _age_account(client, "expired-ping@example.com", days=9)

    body = json.dumps({"zen": "hi"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    r = client.post(
        "/webhook/github/ping-proj", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"},
    )
    assert r.status_code == 200


# ---------- admin sweep endpoint ----------

def test_sweep_requires_admin_key(client, monkeypatch):
    monkeypatch.setattr("app.main.ADMIN_API_KEY", "test-admin-key-123")
    r = client.post("/admin/sweep-expired-trials")
    assert r.status_code == 401

    r = client.post("/admin/sweep-expired-trials", headers={"X-API-Key": "test-admin-key-123"})
    assert r.status_code == 200


def test_sweep_pauses_expired_users_projects(client, monkeypatch, fake_upstream):
    from app import deployer
    from app.config import ADMIN_API_KEY

    paused = []
    monkeypatch.setattr(deployer, "pause_container", lambda slug: (paused.append(slug), True)[1])

    token = _register(client, "sweep-target@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "sweep-proj", "kind": "site"})
    _age_account(client, "sweep-target@example.com", days=8)

    r = client.post("/admin/sweep-expired-trials", headers={"X-API-Key": ADMIN_API_KEY})
    assert r.status_code == 200
    assert "sweep-proj" in r.json()["paused_projects"]
    assert paused == ["sweep-proj"]


def test_sweep_does_not_touch_users_within_trial(client, monkeypatch):
    from app import deployer
    from app.config import ADMIN_API_KEY

    paused = []
    monkeypatch.setattr(deployer, "pause_container", lambda slug: (paused.append(slug), True)[1])

    token = _register(client, "sweep-safe@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "safe-proj", "kind": "site"})
    # no _age_account call — this account is brand new, well within trial

    r = client.post("/admin/sweep-expired-trials", headers={"X-API-Key": ADMIN_API_KEY})
    assert r.status_code == 200
    assert paused == []


def test_sweep_does_not_touch_paid_plans(client, monkeypatch):
    from app import deployer
    from app.config import ADMIN_API_KEY
    from app.models import User

    paused = []
    monkeypatch.setattr(deployer, "pause_container", lambda slug: (paused.append(slug), True)[1])

    token = _register(client, "sweep-paid@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "paid-proj", "kind": "site"})
    db = client.SessionLocal()
    db.query(User).filter_by(email="sweep-paid@example.com").update({"plan": "pro"})
    db.commit()
    db.close()
    _age_account(client, "sweep-paid@example.com", days=100)

    r = client.post("/admin/sweep-expired-trials", headers={"X-API-Key": ADMIN_API_KEY})
    assert r.status_code == 200
    assert paused == []


# ---------- upgrading resumes paused projects ----------

def test_upgrading_resumes_paused_projects(client, monkeypatch):
    from app import billing, deployer

    resumed = []
    monkeypatch.setattr(deployer, "resume_container", lambda slug: resumed.append(slug))
    monkeypatch.setattr(billing, "create_payment", lambda plan, sub_id, payment_method_type=None: {
        "id": "yk_resume_test", "confirmation": {"confirmation_url": "https://yookassa.ru/pay/x"},
    })
    monkeypatch.setattr(billing, "fetch_payment", lambda payment_id: {"id": payment_id, "status": "succeeded"})

    token = _register(client, "resume-on-upgrade@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "resume-proj", "kind": "site"})
    client.post("/billing/subscribe", headers=_auth_headers(token), json={"plan": "pro"})
    r = client.post("/webhook/yookassa", json={"object": {"id": "yk_resume_test", "status": "succeeded"}})
    assert r.status_code == 200
    assert resumed == ["resume-proj"]
