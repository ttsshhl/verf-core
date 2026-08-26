"""Auth primitives: password hashing (raw bcrypt — passlib has a known
incompatibility with recent bcrypt releases) and JWT issue/verify.
"""
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_MINUTES


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False  # malformed hash — never crash auth on bad stored data


def create_access_token(user_id: str) -> str:
    if not JWT_SECRET:
        raise RuntimeError(
            "VERF_JWT_SECRET не задан — сгенерируй длинный случайный секрет и пропиши в .env"
        )
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


class InvalidToken(Exception):
    pass


def decode_access_token(token: str) -> str:
    """Returns the user_id embedded in the token, or raises InvalidToken."""
    if not JWT_SECRET:
        raise InvalidToken("JWT не настроен на сервере")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc))
    if payload.get("typ") is not None:
        raise InvalidToken("Неверный тип токена")  # e.g. a github_connect state token used where a login token belongs
    return payload["sub"]


def create_github_connect_state(user_id: str) -> str:
    """A short-lived, single-purpose token carried through the GitHub OAuth
    redirect round-trip (as the `state` param) to know which VERF user is
    connecting once GitHub calls our callback back. Deliberately a distinct
    "typ" so it can never be reused as a normal login token even if leaked.
    """
    if not JWT_SECRET:
        raise RuntimeError("VERF_JWT_SECRET не задан")
    payload = {
        "sub": user_id,
        "typ": "github_connect",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_github_connect_state(state: str) -> str:
    if not JWT_SECRET:
        raise InvalidToken("JWT не настроен на сервере")
    try:
        payload = jwt.decode(state, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc))
    if payload.get("typ") != "github_connect":
        raise InvalidToken("Неверный тип токена")
    return payload["sub"]
