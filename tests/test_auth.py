import time

import pytest

from app import auth


def test_password_hash_and_verify_roundtrip():
    h = auth.hash_password("correct-horse-battery-staple")
    assert auth.verify_password("correct-horse-battery-staple", h) is True


def test_wrong_password_rejected():
    h = auth.hash_password("correct-horse-battery-staple")
    assert auth.verify_password("wrong-password", h) is False


def test_malformed_hash_never_crashes():
    assert auth.verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_jwt_roundtrip(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    token = auth.create_access_token("user-123")
    user_id = auth.decode_access_token(token)
    assert user_id == "user-123"


def test_jwt_tampered_token_rejected(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    token = auth.create_access_token("user-123")
    tampered = token[:-4] + "abcd"
    with pytest.raises(auth.InvalidToken):
        auth.decode_access_token(tampered)


def test_jwt_wrong_secret_rejected(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET", "secret-a")
    token = auth.create_access_token("user-123")
    monkeypatch.setattr(auth, "JWT_SECRET", "secret-b")
    with pytest.raises(auth.InvalidToken):
        auth.decode_access_token(token)


def test_jwt_expired_token_rejected(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_EXPIRE_MINUTES", -1)  # already expired the instant it's issued
    token = auth.create_access_token("user-123")
    with pytest.raises(auth.InvalidToken):
        auth.decode_access_token(token)


def test_create_token_without_secret_raises(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET", "")
    with pytest.raises(RuntimeError):
        auth.create_access_token("user-123")
