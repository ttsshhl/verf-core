import io
import zipfile

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


def _register(client, email="alice@example.com", password="correct-horse-battery"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_create_project_without_repo_source_then_deploy_via_upload(client, monkeypatch):
    """The core CLI-style flow: create a project with no git URL at all,
    then deploy it purely by uploading a ZIP."""
    from app import deployer
    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")
    monkeypatch.setattr(deployer, "run_container", lambda slug, image, port, env, **kw: "fake-container-id")

    token = _register(client, "cli-user@example.com")
    proj = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "cli-proj", "kind": "site"},
    ).json()
    assert proj["repo_url"] is None

    zip_bytes = _make_zip_bytes({"index.html": "<h1>hi</h1>"})
    r = client.post(
        f"/me/projects/{proj['slug']}/deploy", headers=_auth_headers(token),
        files={"archive": ("project.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 200, r.text
    deployment = r.json()
    assert deployment["status"] == "pending"

    # background task ran synchronously under TestClient — check final state
    final = client.get(f"/me/deployments/{deployment['id']}", headers=_auth_headers(token)).json()
    assert final["status"] == "running"
    assert final["commit_sha"].startswith("upload-")
    assert "Распаковываю" in final["log"]


def test_deploy_upload_rejects_non_zip_extension(client):
    token = _register(client, "notzip@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "notzip-proj", "kind": "site"})
    r = client.post(
        f"/me/projects/notzip-proj/deploy", headers=_auth_headers(token),
        files={"archive": ("project.tar.gz", b"whatever", "application/gzip")},
    )
    assert r.status_code == 400


def test_deploy_upload_rejects_empty_file(client):
    token = _register(client, "empty@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "empty-proj", "kind": "site"})
    r = client.post(
        f"/me/projects/empty-proj/deploy", headers=_auth_headers(token),
        files={"archive": ("project.zip", b"", "application/zip")},
    )
    assert r.status_code == 400


def test_deploy_upload_rejects_oversized_file(client, monkeypatch):
    from app import main as main_module
    monkeypatch.setattr(main_module, "MAX_UPLOAD_SIZE_MB", 0)  # anything at all now exceeds the cap

    token = _register(client, "toobig@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "big-proj", "kind": "site"})
    r = client.post(
        "/me/projects/big-proj/deploy", headers=_auth_headers(token),
        files={"archive": ("project.zip", b"just a few bytes", "application/zip")},
    )
    assert r.status_code == 413


def test_deploy_upload_requires_project_ownership(client):
    token_a = _register(client, "owner-x@example.com")
    token_b = _register(client, "attacker-x@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "owned-proj", "kind": "site"})

    zip_bytes = _make_zip_bytes({"index.html": "hi"})
    r = client.post(
        "/me/projects/owned-proj/deploy", headers=_auth_headers(token_b),
        files={"archive": ("project.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 404  # not found *for this user* — same pattern as delete/deployments


def test_deploy_upload_requires_auth(client):
    zip_bytes = _make_zip_bytes({"index.html": "hi"})
    r = client.post(
        "/me/projects/whatever/deploy",
        files={"archive": ("project.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 401


def test_get_deployment_requires_ownership(client, monkeypatch):
    from app import deployer
    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")
    monkeypatch.setattr(deployer, "run_container", lambda slug, image, port, env, **kw: "fake-container-id")

    token_a = _register(client, "dep-owner@example.com")
    token_b = _register(client, "dep-attacker@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "dep-owned", "kind": "site"})

    zip_bytes = _make_zip_bytes({"index.html": "hi"})
    r = client.post(
        "/me/projects/dep-owned/deploy", headers=_auth_headers(token_a),
        files={"archive": ("project.zip", zip_bytes, "application/zip")},
    )
    deployment_id = r.json()["id"]

    r2 = client.get(f"/me/deployments/{deployment_id}", headers=_auth_headers(token_b))
    assert r2.status_code == 404


def test_get_deployment_unknown_id_404(client):
    token = _register(client, "unknowndep@example.com")
    r = client.get("/me/deployments/does-not-exist", headers=_auth_headers(token))
    assert r.status_code == 404


def test_upload_deploy_failure_is_reflected_in_status(client, monkeypatch):
    """No requirements.txt/package.json/index.html/Dockerfile in the archive
    -> detect_profile fails -> deployment ends up 'failed', not silently lost."""
    token = _register(client, "faildeploy@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "will-fail", "kind": "backend"})

    zip_bytes = _make_zip_bytes({"readme.txt": "no recognizable project files here"})
    r = client.post(
        "/me/projects/will-fail/deploy", headers=_auth_headers(token),
        files={"archive": ("project.zip", zip_bytes, "application/zip")},
    )
    deployment_id = r.json()["id"]
    final = client.get(f"/me/deployments/{deployment_id}", headers=_auth_headers(token)).json()
    assert final["status"] == "failed"
    assert "Не удалось определить тип проекта" in final["log"]
