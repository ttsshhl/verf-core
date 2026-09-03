"""Admin-facing Telegram alerts — new registrations, server health warnings.

Separate from app/notifications.py (user-facing emails): different audience,
different channel. Best-effort/non-fatal by the same convention as email —
callers run this via BackgroundTasks and swallow AlertError.
"""
import requests

from app.config import TELEGRAM_ALERTS_BOT_TOKEN, TELEGRAM_ALERTS_CHAT_ID

TELEGRAM_API = "https://api.telegram.org"


class AlertError(Exception):
    pass


def send_admin_alert(text: str) -> None:
    if not TELEGRAM_ALERTS_BOT_TOKEN or not TELEGRAM_ALERTS_CHAT_ID:
        raise AlertError(
            "Телеграм-оповещения не настроены — пропиши VERF_TELEGRAM_ALERTS_BOT_TOKEN "
            "и VERF_TELEGRAM_ALERTS_CHAT_ID в .env"
        )
    try:
        response = requests.post(
            f"{TELEGRAM_API}/bot{TELEGRAM_ALERTS_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_ALERTS_CHAT_ID, "text": text},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise AlertError(f"Не удалось отправить оповещение: {exc}")
    if response.status_code >= 300:
        raise AlertError(f"Telegram вернул {response.status_code}: {response.text}")


def new_user_registered(email: str) -> None:
    send_admin_alert(f"🆕 Новый пользователь: {email}")
