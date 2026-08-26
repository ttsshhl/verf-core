"""GitHub OAuth + REST API client.

Handles the "Connect GitHub" flow (authorization code -> access token),
listing a connected user's repositories, and creating a push webhook on a
repo on the user's behalf so they never have to configure one by hand.
"""
import requests

from app.config import GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_REDIRECT_URI

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"


class GithubError(Exception):
    pass


def authorize_url(state: str) -> str:
    if not GITHUB_CLIENT_ID:
        raise GithubError(
            "GitHub OAuth не настроен: пропиши VERF_GITHUB_CLIENT_ID и VERF_GITHUB_CLIENT_SECRET в .env "
            "(создать OAuth App: https://github.com/settings/developers)"
        )
    params = (
        f"client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={GITHUB_REDIRECT_URI}"
        f"&scope=repo"
        f"&state={state}"
    )
    return f"{GITHUB_AUTHORIZE_URL}?{params}"


def exchange_code(code: str) -> str:
    """Trades a one-time OAuth code for a long-lived access token."""
    response = requests.post(
        GITHUB_TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": GITHUB_REDIRECT_URI,
        },
        timeout=15,
    )
    if response.status_code >= 300:
        raise GithubError(f"GitHub вернул ошибку {response.status_code}: {response.text}")
    body = response.json()
    if "error" in body:
        raise GithubError(f"GitHub отклонил обмен кода: {body.get('error_description', body['error'])}")
    token = body.get("access_token")
    if not token:
        raise GithubError("GitHub не вернул access_token")
    return token


def fetch_username(access_token: str) -> str:
    response = requests.get(
        f"{GITHUB_API}/user",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    if response.status_code >= 300:
        raise GithubError(f"Не удалось получить профиль GitHub: {response.status_code}")
    return response.json()["login"]


def list_repos(access_token: str) -> list[dict]:
    """Returns the user's repos (owned + collaborator), most recently pushed first."""
    response = requests.get(
        f"{GITHUB_API}/user/repos",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        params={"per_page": 100, "sort": "pushed", "affiliation": "owner,collaborator"},
        timeout=15,
    )
    if response.status_code >= 300:
        raise GithubError(f"Не удалось получить список репозиториев: {response.status_code}")
    return response.json()


def create_webhook(access_token: str, full_name: str, payload_url: str, secret: str) -> bool:
    """Creates a push webhook on the given repo ("owner/repo").

    Returns True on success, False on any failure — this is deliberately
    non-fatal for the caller: project creation should still succeed even if
    auto-configuring the webhook doesn't (e.g. the user only granted access
    to some repos, or the token's scope doesn't cover this one), leaving the
    user able to fall back to manual setup with the returned webhook_secret.
    """
    try:
        response = requests.post(
            f"{GITHUB_API}/repos/{full_name}/hooks",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            json={
                "name": "web",
                "active": True,
                "events": ["push"],
                "config": {"url": payload_url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
            },
            timeout=15,
        )
        return response.status_code in (200, 201)
    except requests.RequestException:
        return False
