import pytest


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
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- connect-state tokens (auth.py) ----------

def test_github_connect_state_roundtrip(monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    state = auth.create_github_connect_state("user-123")
    assert auth.decode_github_connect_state(state) == "user-123"


def test_github_connect_state_rejected_as_normal_login_token(monkeypatch):
    """A connect-state token must never work as a regular Bearer auth token,
    even though both are JWTs signed with the same secret."""
    from app import auth
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    state = auth.create_github_connect_state("user-123")
    with pytest.raises(auth.InvalidToken):
        auth.decode_access_token(state)  # wrong decoder for this token type


def test_normal_login_token_rejected_as_connect_state(monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    login_token = auth.create_access_token("user-123")
    with pytest.raises(auth.InvalidToken):
        auth.decode_github_connect_state(login_token)


# ---------- app.github pure functions ----------

def test_authorize_url_requires_client_id(monkeypatch):
    from app import github as gh
    monkeypatch.setattr(gh, "GITHUB_CLIENT_ID", "")
    with pytest.raises(gh.GithubError):
        gh.authorize_url("some-state")


def test_authorize_url_includes_state(monkeypatch):
    from app import github as gh
    monkeypatch.setattr(gh, "GITHUB_CLIENT_ID", "client123")
    url = gh.authorize_url("my-state-value")
    assert "client_id=client123" in url
    assert "state=my-state-value" in url
    assert "scope=repo" in url


def test_create_webhook_returns_false_on_request_exception(monkeypatch):
    from app import github as gh
    import requests

    def boom(*a, **kw):
        raise requests.RequestException("network broke")

    monkeypatch.setattr(gh.requests, "post", boom)
    result = gh.create_webhook("tok", "owner/repo", "https://x/webhook", "secret")
    assert result is False


def test_create_webhook_returns_true_on_201(monkeypatch):
    from app import github as gh

    class FakeResponse:
        status_code = 201

    monkeypatch.setattr(gh.requests, "post", lambda *a, **kw: FakeResponse())
    assert gh.create_webhook("tok", "owner/repo", "https://x/webhook", "secret") is True


def test_create_webhook_returns_false_on_error_status(monkeypatch):
    from app import github as gh

    class FakeResponse:
        status_code = 422

    monkeypatch.setattr(gh.requests, "post", lambda *a, **kw: FakeResponse())
    assert gh.create_webhook("tok", "owner/repo", "https://x/webhook", "secret") is False


# ---------- end-to-end connect flow ----------

def test_prepare_connect_requires_auth(client):
    r = client.post("/auth/github/prepare-connect")
    assert r.status_code == 401


def test_prepare_connect_returns_nonce(client):
    token = _register(client)
    r = client.post("/auth/github/prepare-connect", headers=_auth_headers(token))
    assert r.status_code == 200
    assert len(r.json()["nonce"]) > 10


def test_start_rejects_unknown_nonce(client):
    r = client.get("/auth/github/start", params={"nonce": "not-a-real-nonce"}, follow_redirects=False)
    assert r.status_code == 400


def test_start_rejects_reused_nonce(client, monkeypatch):
    from app import github as gh
    monkeypatch.setattr(gh, "GITHUB_CLIENT_ID", "client123")

    token = _register(client, email="reuse@example.com")
    nonce = client.post("/auth/github/prepare-connect", headers=_auth_headers(token)).json()["nonce"]

    first = client.get("/auth/github/start", params={"nonce": nonce}, follow_redirects=False)
    assert first.status_code in (302, 307)

    second = client.get("/auth/github/start", params={"nonce": nonce}, follow_redirects=False)
    assert second.status_code == 400  # single-use — can't replay the same nonce


def test_start_redirects_to_github_with_valid_nonce(client, monkeypatch):
    from app import github as gh
    monkeypatch.setattr(gh, "GITHUB_CLIENT_ID", "client123")

    token = _register(client, email="startflow@example.com")
    nonce = client.post("/auth/github/prepare-connect", headers=_auth_headers(token)).json()["nonce"]

    r = client.get("/auth/github/start", params={"nonce": nonce}, follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "github.com/login/oauth/authorize" in r.headers["location"]


def test_callback_rejects_invalid_state(client):
    r = client.get(
        "/auth/github/callback", params={"code": "somecode", "state": "garbage-not-a-jwt"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_callback_saves_token_and_redirects_to_cabinet(client, monkeypatch):
    from app import github as gh, auth as auth_module

    monkeypatch.setattr(auth_module, "JWT_SECRET", "test-secret-for-e2e")
    monkeypatch.setattr(gh, "exchange_code", lambda code: "gh-access-token-xyz")
    monkeypatch.setattr(gh, "fetch_username", lambda token: "octocat")

    token = _register(client, email="connectme@example.com")
    me_before = client.get("/me", headers=_auth_headers(token)).json()
    assert me_before["github_connected"] is False

    nonce = client.post("/auth/github/prepare-connect", headers=_auth_headers(token)).json()["nonce"]
    state = auth_module.create_github_connect_state(_user_id_from_me(client, token))

    r = client.get(
        "/auth/github/callback", params={"code": "one-time-code", "state": state},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "cabinet." in r.headers["location"]
    assert "github=connected" in r.headers["location"]

    me_after = client.get("/me", headers=_auth_headers(token)).json()
    assert me_after["github_connected"] is True
    assert me_after["github_username"] == "octocat"


def _user_id_from_me(client, token):
    # helper: decode user id via the token itself, since /me doesn't expose it directly beyond `id`
    return client.get("/me", headers=_auth_headers(token)).json()["id"]


def test_disconnect_github(client, monkeypatch):
    from app import github as gh, auth as auth_module
    monkeypatch.setattr(auth_module, "JWT_SECRET", "test-secret-for-e2e")
    monkeypatch.setattr(gh, "exchange_code", lambda code: "tok")
    monkeypatch.setattr(gh, "fetch_username", lambda token: "someuser")

    token = _register(client, email="disconnect@example.com")
    user_id = _user_id_from_me(client, token)
    state = auth_module.create_github_connect_state(user_id)
    client.get("/auth/github/callback", params={"code": "c", "state": state}, follow_redirects=False)

    assert client.get("/me", headers=_auth_headers(token)).json()["github_connected"] is True

    r = client.delete("/me/github", headers=_auth_headers(token))
    assert r.status_code == 200
    assert client.get("/me", headers=_auth_headers(token)).json()["github_connected"] is False


# ---------- repo listing ----------

def test_list_repos_requires_github_connected(client):
    token = _register(client, email="norepo@example.com")
    r = client.get("/me/github/repos", headers=_auth_headers(token))
    assert r.status_code == 400


def test_list_repos_returns_mapped_repos(client, monkeypatch):
    from app import github as gh, auth as auth_module
    monkeypatch.setattr(auth_module, "JWT_SECRET", "test-secret-for-e2e")
    monkeypatch.setattr(gh, "exchange_code", lambda code: "tok")
    monkeypatch.setattr(gh, "fetch_username", lambda token: "someuser")

    token = _register(client, email="hasrepos@example.com")
    user_id = _user_id_from_me(client, token)
    state = auth_module.create_github_connect_state(user_id)
    client.get("/auth/github/callback", params={"code": "c", "state": state}, follow_redirects=False)

    monkeypatch.setattr(gh, "list_repos", lambda token: [
        {"name": "my-bot", "full_name": "someuser/my-bot", "default_branch": "main", "private": False},
        {"name": "secret-proj", "full_name": "someuser/secret-proj", "default_branch": "master", "private": True},
    ])

    r = client.get("/me/github/repos", headers=_auth_headers(token))
    assert r.status_code == 200
    repos = r.json()
    assert len(repos) == 2
    assert repos[0]["full_name"] == "someuser/my-bot"
    assert repos[1]["private"] is True


# ---------- project creation via repo_full_name + auto-webhook ----------

def test_create_project_with_repo_full_name_constructs_url(client, monkeypatch):
    from app import github as gh, auth as auth_module
    monkeypatch.setattr(auth_module, "JWT_SECRET", "test-secret-for-e2e")
    monkeypatch.setattr(gh, "exchange_code", lambda code: "tok")
    monkeypatch.setattr(gh, "fetch_username", lambda token: "someuser")
    monkeypatch.setattr(gh, "create_webhook", lambda *a, **kw: True)

    token = _register(client, email="picker@example.com")
    user_id = _user_id_from_me(client, token)
    state = auth_module.create_github_connect_state(user_id)
    client.get("/auth/github/callback", params={"code": "c", "state": state}, follow_redirects=False)

    r = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "picked-proj", "repo_full_name": "someuser/picked-proj", "branch": "main", "kind": "bot"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo_url"] == "https://github.com/someuser/picked-proj.git"
    assert body["webhook_auto_configured"] is True


def test_create_project_webhook_failure_is_non_fatal(client, monkeypatch):
    from app import github as gh, auth as auth_module
    monkeypatch.setattr(auth_module, "JWT_SECRET", "test-secret-for-e2e")
    monkeypatch.setattr(gh, "exchange_code", lambda code: "tok")
    monkeypatch.setattr(gh, "fetch_username", lambda token: "someuser")
    monkeypatch.setattr(gh, "create_webhook", lambda *a, **kw: False)  # simulate GitHub API failure

    token = _register(client, email="webhookfail@example.com")
    user_id = _user_id_from_me(client, token)
    state = auth_module.create_github_connect_state(user_id)
    client.get("/auth/github/callback", params={"code": "c", "state": state}, follow_redirects=False)

    r = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "fail-proj", "repo_full_name": "someuser/fail-proj", "branch": "main", "kind": "bot"},
    )
    assert r.status_code == 200, r.text  # project creation still succeeds
    assert r.json()["webhook_auto_configured"] is False  # but the flag reflects the failure honestly


def test_create_project_without_github_connected_skips_auto_webhook(client):
    token = _register(client, email="noconnect@example.com")
    r = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "manual-proj", "repo_full_name": "someone/manual-proj", "branch": "main", "kind": "bot"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["webhook_auto_configured"] is False


def test_create_project_without_repo_source_is_valid(client):
    """A project with no repo_url and no repo_full_name is legitimate now —
    it's a CLI/ZIP-only project with no git remote at all."""
    token = _register(client, email="norepoinfo@example.com")
    r = client.post(
        "/me/projects", headers=_auth_headers(token),
        json={"slug": "no-repo-info", "branch": "main", "kind": "bot"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["repo_url"] is None
