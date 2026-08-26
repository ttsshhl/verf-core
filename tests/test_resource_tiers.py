import hashlib
import hmac
import json
import subprocess
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


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _register(client, email, password="correct-horse-battery"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"]


def _push_and_capture_run_container_call(client, monkeypatch, fake_upstream, token, slug):
    """Registers a project, sends a signed push webhook, and returns the
    kwargs run_container was called with — without needing a real Docker
    daemon (build_image/run_container are mocked, everything upstream of
    them — auth, ownership, plan resolution — is real).
    """
    from app import deployer

    captured = {}

    def fake_run_container(slug, image, port, env, mem_limit=None, cpu_quota=None):
        captured["mem_limit"] = mem_limit
        captured["cpu_quota"] = cpu_quota
        return "fake-container-id"

    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")
    monkeypatch.setattr(deployer, "run_container", fake_run_container)

    proj = client.post(
        "/me/projects", headers={"Authorization": f"Bearer {token}"},
        json={"slug": slug, "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()

    body = json.dumps({"ref": "refs/heads/main", "after": "x"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    r = client.post(
        f"/webhook/github/{slug}", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert r.status_code == 200
    return captured


def test_free_plan_gets_free_tier_resources(client, monkeypatch, fake_upstream):
    token = _register(client, "free-tier@example.com")
    captured = _push_and_capture_run_container_call(client, monkeypatch, fake_upstream, token, "free-proj")
    assert captured["mem_limit"] == "512m"
    assert captured["cpu_quota"] == 50_000


def test_pro_plan_gets_pro_tier_resources(client, monkeypatch, fake_upstream):
    from app.db import SessionLocal  # noqa: F401 — not used directly; kept for parity, real session below
    from app.models import User

    token = _register(client, "pro-tier@example.com")
    db = client.SessionLocal()
    user = db.query(User).filter_by(email="pro-tier@example.com").first()
    user.plan = "pro"
    db.commit()
    db.close()

    captured = _push_and_capture_run_container_call(client, monkeypatch, fake_upstream, token, "pro-proj")
    assert captured["mem_limit"] == "2048m"
    assert captured["cpu_quota"] == 100_000


def test_business_plan_gets_business_tier_resources(client, monkeypatch, fake_upstream):
    from app.models import User

    token = _register(client, "biz-tier@example.com")
    db = client.SessionLocal()
    user = db.query(User).filter_by(email="biz-tier@example.com").first()
    user.plan = "business"
    db.commit()
    db.close()

    captured = _push_and_capture_run_container_call(client, monkeypatch, fake_upstream, token, "biz-proj")
    assert captured["mem_limit"] == "8192m"
    assert captured["cpu_quota"] == 200_000


def test_admin_created_project_with_no_owner_gets_free_tier_default(client, monkeypatch, fake_upstream):
    """Admin-created projects (via /projects, no owner) shouldn't crash —
    they fall back to the free-tier default rather than erroring on a
    missing owner."""
    from app import deployer

    captured = {}

    def fake_run_container(slug, image, port, env, mem_limit=None, cpu_quota=None):
        captured["mem_limit"] = mem_limit
        captured["cpu_quota"] = cpu_quota
        return "fake-container-id"

    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")
    monkeypatch.setattr(deployer, "run_container", fake_run_container)

    proj = client.post(
        "/projects", json={"slug": "admin-proj", "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()

    body = json.dumps({"ref": "refs/heads/main", "after": "x"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    r = client.post(
        "/webhook/github/admin-proj", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert r.status_code == 200
    assert captured["mem_limit"] == "512m"
    assert captured["cpu_quota"] == 50_000
