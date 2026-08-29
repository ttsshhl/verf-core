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


def _register(client, email, password="Correct-Horse1!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_get_logs_requires_auth(client):
    r = client.get("/me/projects/whatever/logs")
    assert r.status_code == 401


def test_get_logs_requires_ownership(client):
    token_a = _register(client, "logsowner@example.com")
    token_b = _register(client, "logsattacker@example.com")
    client.post("/me/projects", headers=_auth_headers(token_a), json={"slug": "log-proj", "kind": "bot"})

    r = client.get("/me/projects/log-proj/logs", headers=_auth_headers(token_b))
    assert r.status_code == 404


def test_get_logs_404_when_container_never_deployed(client, monkeypatch):
    from app import deployer

    def fake_get_logs(slug, tail=200):
        raise deployer.DeployError("Контейнер не найден — возможно, ещё не было ни одного успешного деплоя")

    monkeypatch.setattr(deployer, "get_container_logs", fake_get_logs)

    token = _register(client, "neverdeployedlogs@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "fresh-log-proj", "kind": "bot"})

    r = client.get("/me/projects/fresh-log-proj/logs", headers=_auth_headers(token))
    assert r.status_code == 404


def test_get_logs_returns_container_output(client, monkeypatch):
    from app import deployer

    captured = {}

    def fake_get_logs(slug, tail=200):
        captured["slug"] = slug
        captured["tail"] = tail
        return "INFO: bot started\nTraceback (most recent call last):\nKeyError: 'BOT_TOKEN'\n"

    monkeypatch.setattr(deployer, "get_container_logs", fake_get_logs)

    token = _register(client, "haslogs@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "has-logs-proj", "kind": "bot"})

    r = client.get("/me/projects/has-logs-proj/logs", headers=_auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert "KeyError" in body["logs"]
    assert captured["slug"] == "has-logs-proj"
    assert captured["tail"] == 200  # default


def test_get_logs_respects_lines_query_param(client, monkeypatch):
    from app import deployer

    captured = {}

    def fake_get_logs(slug, tail=200):
        captured["tail"] = tail
        return "some log line\n"

    monkeypatch.setattr(deployer, "get_container_logs", fake_get_logs)

    token = _register(client, "customlines@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "custom-lines-proj", "kind": "bot"})

    r = client.get("/me/projects/custom-lines-proj/logs?lines=50", headers=_auth_headers(token))
    assert r.status_code == 200
    assert captured["tail"] == 50


def test_get_logs_caps_lines_at_1000(client, monkeypatch):
    """Prevents a user from requesting an absurdly large tail and hammering
    the Docker daemon / response size."""
    from app import deployer

    captured = {}

    def fake_get_logs(slug, tail=200):
        captured["tail"] = tail
        return "line\n"

    monkeypatch.setattr(deployer, "get_container_logs", fake_get_logs)

    token = _register(client, "hugelines@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "huge-lines-proj", "kind": "bot"})

    r = client.get("/me/projects/huge-lines-proj/logs?lines=999999", headers=_auth_headers(token))
    assert r.status_code == 200
    assert captured["tail"] == 1000


def test_get_logs_floors_lines_at_1(client, monkeypatch):
    from app import deployer

    captured = {}

    def fake_get_logs(slug, tail=200):
        captured["tail"] = tail
        return "line\n"

    monkeypatch.setattr(deployer, "get_container_logs", fake_get_logs)

    token = _register(client, "zerolines@example.com")
    client.post("/me/projects", headers=_auth_headers(token), json={"slug": "zero-lines-proj", "kind": "bot"})

    r = client.get("/me/projects/zero-lines-proj/logs?lines=0", headers=_auth_headers(token))
    assert r.status_code == 200
    assert captured["tail"] == 1
