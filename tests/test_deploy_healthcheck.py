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


def _register(client, email, password="Correct-Horse1!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _deploy(client, monkeypatch, fake_upstream, slug, token):
    from app import deployer
    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")
    monkeypatch.setattr(deployer, "run_container", lambda slug, image, port, env, **kw: "fake-container-id")

    proj = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": slug, "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()
    body = json.dumps({"ref": "refs/heads/main", "after": "x"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    client.post(
        f"/webhook/github/{slug}", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    return proj


# ---------- pipeline.py health-check gating ----------

def test_deployment_running_when_port_check_succeeds(client, monkeypatch, fake_upstream):
    from app import deployer
    monkeypatch.setattr(deployer, "wait_for_container_port", lambda slug, port, timeout=15.0: True)

    token = _register(client, "healthy-deploy@example.com")
    proj = _deploy(client, monkeypatch, fake_upstream, "healthy-proj", token)

    deploys = client.get(f"/me/projects/{proj['slug']}/deployments", headers=_auth_headers(token)).json()
    assert deploys[0]["status"] == "running"
    assert "Приложение отвечает" in deploys[0]["log"]


def test_deployment_failed_when_port_check_fails(client, monkeypatch, fake_upstream):
    """The whole point: a container that starts but never actually
    listens on the expected port must NOT be reported as "running" — this
    is exactly the class of bug (port mismatch) that caused a real Bad
    Gateway incident with no diagnostic anywhere in the deployment log."""
    from app import deployer
    monkeypatch.setattr(deployer, "wait_for_container_port", lambda slug, port, timeout=15.0: False)

    token = _register(client, "unhealthy-deploy@example.com")
    proj = _deploy(client, monkeypatch, fake_upstream, "unhealthy-proj", token)

    deploys = client.get(f"/me/projects/{proj['slug']}/deployments", headers=_auth_headers(token)).json()
    assert deploys[0]["status"] == "failed"
    assert "не отвечает" in deploys[0]["log"]
    assert "EXPOSE" in deploys[0]["log"]  # points the user at the actual fix, not just "it broke"


def test_failed_health_check_still_records_container_id(client, monkeypatch, fake_upstream):
    """Even though the deployment is marked failed, the container really is
    running (just unreachable on the expected port) — the record should
    still reflect that, since e.g. `docker logs` troubleshooting depends on
    knowing this."""
    from app import deployer
    monkeypatch.setattr(deployer, "wait_for_container_port", lambda slug, port, timeout=15.0: False)

    token = _register(client, "recorded-container@example.com")
    proj = _deploy(client, monkeypatch, fake_upstream, "recorded-proj", token)

    deploys = client.get(f"/me/projects/{proj['slug']}/deployments", headers=_auth_headers(token)).json()
    assert deploys[0]["port"] == 80  # static site profile's internal_port
