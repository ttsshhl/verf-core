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


# ---------- validation ----------

def test_set_domain_rejects_malformed_input(client):
    token = _register(client, "baddomain@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "proj1", "kind": "site"})
    r = client.post("/me/projects/proj1/domain", headers=_auth_headers(token), json={"domain": "not a domain!!"})
    assert r.status_code == 400


def test_set_domain_rejects_own_subdomain(client):
    """Can't claim a *.verfdeploy.ru address through this endpoint — that's
    already automatically the project's own subdomain."""
    from app.config import DOMAIN_SUFFIX
    token = _register(client, "ownsub@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "proj1", "kind": "site"})
    r = client.post(
        "/me/projects/proj1/domain", headers=_auth_headers(token),
        json={"domain": f"somethingelse.{DOMAIN_SUFFIX}"},
    )
    assert r.status_code == 400


def test_set_domain_accepts_valid_domain(client):
    token = _register(client, "gooddomain@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "proj1", "kind": "site"})
    r = client.post("/me/projects/proj1/domain", headers=_auth_headers(token), json={"domain": "example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["custom_domain"] == "example.com"
    assert body["custom_domain_url"] == "https://example.com"


def test_set_domain_lowercases_and_strips(client):
    token = _register(client, "caseinsensitive@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "proj1", "kind": "site"})
    r = client.post(
        "/me/projects/proj1/domain", headers=_auth_headers(token), json={"domain": "  Example.COM  "},
    )
    assert r.status_code == 200
    assert r.json()["custom_domain"] == "example.com"


# ---------- ownership ----------

def test_set_domain_requires_ownership(client):
    token_a = _register(client, "domainowner@example.com")
    token_b = _register(client, "domainattacker@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "owned-proj", "kind": "site"})
    r = client.post(
        "/me/projects/owned-proj/domain", headers=_auth_headers(token_b), json={"domain": "example.com"},
    )
    assert r.status_code == 404


def test_set_domain_requires_auth(client):
    r = client.post("/me/projects/whatever/domain", json={"domain": "example.com"})
    assert r.status_code == 401


# ---------- uniqueness ----------

def test_set_domain_rejects_domain_already_claimed_by_another_project(client):
    token_a = _register(client, "first@example.com")
    token_b = _register(client, "second@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "proj-a", "kind": "site"})
    client.post("/me/projects", headers=_auth_headers(token_b), json={"slug": "proj-b", "kind": "site"})

    r1 = client.post("/me/projects/proj-a/domain", headers=_auth_headers(token_a), json={"domain": "shared.com"})
    assert r1.status_code == 200

    r2 = client.post("/me/projects/proj-b/domain", headers=_auth_headers(token_b), json={"domain": "shared.com"})
    assert r2.status_code == 409


def test_can_reassign_same_domain_to_same_project(client):
    """Setting the same domain again on the same project (not a different
    one) should not trip the uniqueness check against itself."""
    token = _register(client, "resubmit@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "proj1", "kind": "site"})
    client.post("/me/projects/proj1/domain", headers=_auth_headers(token), json={"domain": "example.com"})
    r = client.post("/me/projects/proj1/domain", headers=_auth_headers(token), json={"domain": "example.com"})
    assert r.status_code == 200


# ---------- removal ----------

def test_remove_domain(client):
    token = _register(client, "removedomain@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "proj1", "kind": "site"})
    client.post("/me/projects/proj1/domain", headers=_auth_headers(token), json={"domain": "example.com"})

    r = client.delete("/me/projects/proj1/domain", headers=_auth_headers(token))
    assert r.status_code == 200
    assert r.json()["custom_domain"] is None
    assert r.json()["custom_domain_url"] is None


def test_remove_domain_requires_ownership(client):
    token_a = _register(client, "removeowner@example.com")
    token_b = _register(client, "removeattacker@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "proj1", "kind": "site"})
    client.post("/me/projects/proj1/domain", headers=_auth_headers(token_a), json={"domain": "example.com"})

    r = client.delete("/me/projects/proj1/domain", headers=_auth_headers(token_b))
    assert r.status_code == 404


def test_freed_domain_can_be_claimed_by_someone_else(client):
    token_a = _register(client, "releaser@example.com")
    token_b = _register(client, "claimer@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "proj-a", "kind": "site"})
    client.post("/me/projects", headers=_auth_headers(token_b), json={"slug": "proj-b", "kind": "site"})

    client.post("/me/projects/proj-a/domain", headers=_auth_headers(token_a), json={"domain": "up-for-grabs.com"})
    client.delete("/me/projects/proj-a/domain", headers=_auth_headers(token_a))

    r = client.post(
        "/me/projects/proj-b/domain", headers=_auth_headers(token_b), json={"domain": "up-for-grabs.com"},
    )
    assert r.status_code == 200


# ---------- immediate application to a running container ----------

def test_setting_domain_on_running_project_triggers_redeploy_with_domain(client, monkeypatch, fake_upstream):
    from app import deployer

    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")

    captured = {}

    def fake_run_container(slug, image, port, env, mem_limit=None, cpu_quota=None, custom_domain=None):
        captured["custom_domain"] = custom_domain
        return "fake-container-id"

    monkeypatch.setattr(deployer, "run_container", fake_run_container)

    token = _register(client, "livedeploy@example.com")
    proj = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "live-proj", "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()

    body = json.dumps({"ref": "refs/heads/main", "after": "x"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    client.post(
        "/webhook/github/live-proj", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    # first deploy ran with no custom domain yet
    assert captured["custom_domain"] is None

    r = client.post(
        "/me/projects/live-proj/domain", headers=_auth_headers(token), json={"domain": "example.com"},
    )
    assert r.status_code == 200
    # setting the domain triggered an immediate container recreate carrying it
    assert captured["custom_domain"] == "example.com"


def test_setting_domain_on_never_deployed_project_does_not_crash(client):
    """No deployment exists yet — saving the domain should succeed and just
    apply on the project's first real deploy instead of erroring out."""
    token = _register(client, "neverdeployed@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "fresh-proj", "kind": "site"})
    r = client.post(
        "/me/projects/fresh-proj/domain", headers=_auth_headers(token), json={"domain": "example.com"},
    )
    assert r.status_code == 200
    assert r.json()["custom_domain"] == "example.com"


# ---------- /server-info (used by cabinet for DNS instructions) ----------

def test_server_info_returns_ip_and_domain_suffix(client, monkeypatch):
    from app import main as main_module
    from app.config import DOMAIN_SUFFIX

    class FakeResponse:
        def json(self):
            return {"ip": "1.2.3.4"}

    monkeypatch.setattr("requests.get", lambda *a, **kw: FakeResponse())
    monkeypatch.setattr(main_module, "_server_ip_cache", {"ip": None, "fetched_at": 0.0})

    r = client.get("/server-info")
    assert r.status_code == 200
    body = r.json()
    assert body["ip"] == "1.2.3.4"
    assert body["domain_suffix"] == DOMAIN_SUFFIX


def test_server_info_caches_and_does_not_refetch_immediately(client, monkeypatch):
    from app import main as main_module
    import time as time_module

    call_count = {"n": 0}

    class FakeResponse:
        def json(self):
            call_count["n"] += 1
            return {"ip": "5.6.7.8"}

    monkeypatch.setattr("requests.get", lambda *a, **kw: FakeResponse())
    monkeypatch.setattr(main_module, "_server_ip_cache", {"ip": None, "fetched_at": 0.0})

    client.get("/server-info")
    client.get("/server-info")
    assert call_count["n"] == 1  # second call served from cache, no re-fetch


def test_server_info_survives_external_lookup_failure(client, monkeypatch):
    from app import main as main_module

    def boom(*a, **kw):
        raise Exception("network down")

    monkeypatch.setattr("requests.get", boom)
    monkeypatch.setattr(main_module, "_server_ip_cache", {"ip": None, "fetched_at": 0.0})

    r = client.get("/server-info")
    assert r.status_code == 200
    assert r.json()["ip"] is None  # graceful — no crash, just no IP yet


# ---------- env vars ----------

def test_create_project_with_env_vars(client):
    token = _register(client, "envcreate@example.com")
    r = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "envproj", "kind": "bot", "env": {"BOT_TOKEN": "secret123", "DEBUG": "false"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["env"] == {"BOT_TOKEN": "secret123", "DEBUG": "false"}


def test_create_project_without_env_defaults_to_empty(client):
    token = _register(client, "envdefault@example.com")
    r = client.post("/me/projects", headers=_auth_headers(token), json={"slug": "noenv", "kind": "bot"})
    assert r.status_code == 200
    assert r.json()["env"] == {}


def test_update_env(client):
    token = _register(client, "envupdate@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "proj1", "kind": "bot"})

    r = client.put(
        "/me/projects/proj1/env", headers=_auth_headers(token),
        json={"env": {"API_KEY": "abc", "PORT": "8080"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["env"] == {"API_KEY": "abc", "PORT": "8080"}


def test_update_env_replaces_not_merges(client):
    """Setting env should replace the whole set, not merge with the old
    one — same semantics as PUT everywhere else in this API."""
    token = _register(client, "envreplace@example.com")
    client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "proj1", "kind": "bot", "env": {"OLD_KEY": "old"}},
    )
    r = client.put(
        "/me/projects/proj1/env", headers=_auth_headers(token), json={"env": {"NEW_KEY": "new"}},
    )
    assert r.status_code == 200
    assert r.json()["env"] == {"NEW_KEY": "new"}


def test_update_env_to_empty_clears_it(client):
    token = _register(client, "envclear@example.com")
    client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "proj1", "kind": "bot", "env": {"KEY": "val"}},
    )
    r = client.put("/me/projects/proj1/env", headers=_auth_headers(token), json={"env": {}})
    assert r.status_code == 200
    assert r.json()["env"] == {}


def test_update_env_requires_ownership(client):
    token_a = _register(client, "envowner@example.com")
    token_b = _register(client, "envattacker@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "proj1", "kind": "bot"})

    r = client.put(
        "/me/projects/proj1/env", headers=_auth_headers(token_b), json={"env": {"X": "y"}},
    )
    assert r.status_code == 404


def test_update_env_requires_auth(client):
    r = client.put("/me/projects/whatever/env", json={"env": {"X": "y"}})
    assert r.status_code == 401


def test_update_env_on_running_project_triggers_redeploy_with_new_env(client, monkeypatch, fake_upstream):
    from app import deployer

    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")

    captured = {}

    def fake_run_container(slug, image, port, env, mem_limit=None, cpu_quota=None, custom_domain=None):
        captured["env"] = env
        return "fake-container-id"

    monkeypatch.setattr(deployer, "run_container", fake_run_container)

    token = _register(client, "envlive@example.com")
    proj = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "live-env-proj", "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()

    body = json.dumps({"ref": "refs/heads/main", "after": "x"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    client.post(
        "/webhook/github/live-env-proj", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert captured["env"] == {}

    client.put(
        "/me/projects/live-env-proj/env", headers=_auth_headers(token), json={"env": {"NEW_TOKEN": "xyz"}},
    )
    assert captured["env"] == {"NEW_TOKEN": "xyz"}


def test_update_env_on_never_deployed_project_does_not_crash(client):
    token = _register(client, "envfresh@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "fresh-env-proj", "kind": "bot"})
    r = client.put(
        "/me/projects/fresh-env-proj/env", headers=_auth_headers(token), json={"env": {"X": "y"}},
    )
    assert r.status_code == 200
    assert r.json()["env"] == {"X": "y"}


# ---------- deleting a project that has deployment history ----------

def test_delete_project_with_deployment_history(client, monkeypatch, fake_upstream):
    """Regression test: deleting a project that has at least one real
    Deployment row used to fail with
    'NOT NULL constraint failed: deployments.project_id' — SQLAlchemy's
    default relationship behaviour tries to null out the FK on child rows
    instead of deleting them. cascade="all, delete-orphan" on
    Project.deployments fixes this."""
    from app import deployer
    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")
    monkeypatch.setattr(deployer, "run_container", lambda slug, image, port, env, **kw: "fake-container-id")
    monkeypatch.setattr(deployer, "stop_and_remove", lambda slug: None)

    token = _register(client, "deletewithhistory@example.com")
    proj = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "has-deploys", "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()

    body = json.dumps({"ref": "refs/heads/main", "after": "x"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    r = client.post(
        "/webhook/github/has-deploys", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert r.status_code == 200

    deploys = client.get("/me/projects/has-deploys/deployments", headers=_auth_headers(token)).json()
    assert len(deploys) == 1  # confirms a real Deployment row exists before we try deleting

    r = client.delete("/me/projects/has-deploys", headers=_auth_headers(token))
    assert r.status_code == 200, r.text


# ---------- retry-cert self-service (nudges Traefik to retry ACME) ----------

def test_retry_cert_requires_ownership(client):
    token_a = _register(client, "retryowner@example.com")
    token_b = _register(client, "retryattacker@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "proj1", "kind": "site"})
    client.post("/me/projects/proj1/domain", headers=_auth_headers(token_a), json={"domain": "example.com"})

    r = client.post("/me/projects/proj1/domain/retry-cert", headers=_auth_headers(token_b))
    assert r.status_code == 404


def test_retry_cert_requires_auth(client):
    r = client.post("/me/projects/whatever/domain/retry-cert")
    assert r.status_code == 401


def test_retry_cert_requires_domain_to_be_set(client):
    token = _register(client, "retrynodomain@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "proj1", "kind": "site"})
    r = client.post("/me/projects/proj1/domain/retry-cert", headers=_auth_headers(token))
    assert r.status_code == 400


def test_retry_cert_recreates_running_container(client, monkeypatch, fake_upstream):
    """The actual point of this endpoint: recreating the container gives
    Traefik's Docker provider a fresh event to notice and retry the ACME
    challenge for a domain whose DNS wasn't ready the first time."""
    from app import deployer
    monkeypatch.setattr(deployer, "build_image", lambda slug, dep_id: f"verf/{slug}:{dep_id}")

    recreate_count = {"n": 0}

    def fake_run_container(slug, image, port, env, mem_limit=None, cpu_quota=None, custom_domain=None):
        recreate_count["n"] += 1
        return "fake-container-id"

    monkeypatch.setattr(deployer, "run_container", fake_run_container)

    token = _register(client, "retrytrigger@example.com")
    proj = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "retry-proj", "repo_url": str(fake_upstream), "branch": "main", "kind": "site"},
    ).json()

    body = json.dumps({"ref": "refs/heads/main", "after": "x"}).encode()
    sig = _sign(proj["webhook_secret"], body)
    client.post(
        "/webhook/github/retry-proj", content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    client.post("/me/projects/retry-proj/domain", headers=_auth_headers(token), json={"domain": "example.com"})
    calls_after_attach = recreate_count["n"]
    assert calls_after_attach >= 2  # initial deploy + redeploy-on-domain-attach

    r = client.post("/me/projects/retry-proj/domain/retry-cert", headers=_auth_headers(token))
    assert r.status_code == 200, r.text
    assert recreate_count["n"] == calls_after_attach + 1  # exactly one more recreate, for the retry
