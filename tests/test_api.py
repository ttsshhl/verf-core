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
    (repo / "requirements.txt").write_text("fastapi\n")
    (repo / "main.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def client(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import builder
    from app.db import Base, get_db
    from app.main import app

    # Isolated workspace dir for git clones
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")

    # Isolated DB: fresh engine/tables per test, wired in via dependency override
    # rather than mutating global module state (which would leak between tests).
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


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_project(client):
    r = client.post("/projects", json={
        "slug": "my-bot",
        "repo_url": "https://github.com/example/my-bot.git",
        "branch": "main",
        "kind": "bot",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "my-bot"
    assert body["url"] == "https://my-bot.verf.dev"
    assert len(body["webhook_secret"]) > 20


def test_create_duplicate_slug_rejected(client):
    payload = {"slug": "dupe", "repo_url": "https://x.git", "branch": "main", "kind": "bot"}
    assert client.post("/projects", json=payload).status_code == 200
    r2 = client.post("/projects", json=payload)
    assert r2.status_code == 409


def test_invalid_slug_rejected(client):
    r = client.post("/projects", json={"slug": "A", "repo_url": "https://x.git", "branch": "main", "kind": "bot"})
    assert r.status_code == 422  # fails the slug pattern (uppercase not allowed)


def test_webhook_wrong_signature_rejected(client, fake_upstream):
    proj = client.post("/projects", json={
        "slug": "sec-test", "repo_url": str(fake_upstream), "branch": "main", "kind": "bot",
    }).json()

    body = json.dumps({"ref": "refs/heads/main", "after": "abc"}).encode()
    r = client.post(
        f"/webhook/github/{proj['slug']}",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=deadbeef", "X-GitHub-Event": "push"},
    )
    assert r.status_code == 401


def test_webhook_ping_event(client, fake_upstream):
    proj = client.post("/projects", json={
        "slug": "ping-test", "repo_url": str(fake_upstream), "branch": "main", "kind": "bot",
    }).json()

    body = b"{}"
    sig = _sign(proj["webhook_secret"], body)
    r = client.post(
        f"/webhook/github/{proj['slug']}",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"},
    )
    assert r.status_code == 200
    assert r.json() == {"pong": True}


def test_webhook_push_triggers_deployment_record(client, fake_upstream, monkeypatch):
    # Mock only the Docker-dependent half of the pipeline — everything else
    # (webhook auth, git clone, project-type detection, DB writes) is real.
    from app import deployer
    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")
    monkeypatch.setattr(deployer, "run_container", lambda slug, image, port, env, **kw: "fake_container_id_1234")

    proj = client.post("/projects", json={
        "slug": "push-test", "repo_url": str(fake_upstream), "branch": "main", "kind": "bot",
    }).json()

    payload = {"ref": "refs/heads/main", "after": "irrelevant-because-real-git-computes-it"}
    body = json.dumps(payload).encode()
    sig = _sign(proj["webhook_secret"], body)

    r = client.post(
        f"/webhook/github/{proj['slug']}",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"

    deployments = client.get(f"/projects/{proj['slug']}/deployments").json()
    assert len(deployments) == 1
    d = deployments[0]
    assert d["status"] == "running"
    assert d["commit_sha"] is not None and len(d["commit_sha"]) == 40
    assert "Собираю образ" in d["log"]
    assert "Живой" in d["log"]


def test_webhook_wrong_branch_ignored(client, fake_upstream):
    proj = client.post("/projects", json={
        "slug": "branch-test", "repo_url": str(fake_upstream), "branch": "main", "kind": "bot",
    }).json()

    payload = {"ref": "refs/heads/develop", "after": "abc"}
    body = json.dumps(payload).encode()
    sig = _sign(proj["webhook_secret"], body)
    r = client.post(
        f"/webhook/github/{proj['slug']}",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert r.status_code == 200
    assert "ignored" in r.json()


def test_delete_project(client, fake_upstream, monkeypatch):
    from app import deployer
    monkeypatch.setattr(deployer, "stop_and_remove", lambda slug: None)

    proj = client.post("/projects", json={
        "slug": "del-test", "repo_url": str(fake_upstream), "branch": "main", "kind": "bot",
    }).json()
    r = client.delete(f"/projects/{proj['slug']}")
    assert r.status_code == 200
    assert client.get("/projects").json() == []
