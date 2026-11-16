"""Transactional email — welcome on registration, confirmation on payment.

Sends over SMTP (Yandex Mail by default). Every call site treats sending as
best-effort: a broken SMTP config or a transient network hiccup must never
block registration or payment activation, so callers always run this via
FastAPI's BackgroundTasks and swallow EmailError rather than propagating it.
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from app import config
from app.config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_EMAIL, SMTP_FROM_NAME


class EmailError(Exception):
    pass


class EmailNotConfigured(EmailError):
    """Mail isn't set up at all — expected in dev, not worth an admin alert."""


def _post_json(url: str, headers: dict, payload: dict) -> requests.Response:
    try:
        return requests.post(url, headers=headers, json=payload, timeout=15)
    except requests.RequestException as exc:
        raise EmailError(f"Не удалось связаться с почтовым сервисом: {exc}")


def _send_brevo(to: str, subject: str, html_body: str) -> None:
    r = _post_json(
        "https://api.brevo.com/v3/smtp/email",
        {"api-key": config.EMAIL_API_KEY, "accept": "application/json"},
        {"sender": {"name": SMTP_FROM_NAME, "email": config.EMAIL_FROM},
         "to": [{"email": to}], "subject": subject, "htmlContent": html_body},
    )
    if r.status_code >= 300:
        raise EmailError(f"Brevo вернул {r.status_code}: {r.text[:200]}")


def _send_unisender(to: str, subject: str, html_body: str) -> None:
    r = _post_json(
        "https://goapi.unisender.ru/ru/transactional/api/v1/email/send.json",
        {"X-API-KEY": config.EMAIL_API_KEY},
        {"message": {"recipients": [{"email": to}], "subject": subject,
                     "from_email": config.EMAIL_FROM, "from_name": SMTP_FROM_NAME,
                     "skip_unsubscribe": 1, "body": {"html": html_body}}},
    )
    if r.status_code >= 300:
        raise EmailError(f"Unisender Go вернул {r.status_code}: {r.text[:200]}")


def send_email(to: str, subject: str, html_body: str) -> None:
    provider = config.EMAIL_PROVIDER or "smtp"
    if provider in ("brevo", "unisender"):
        if not config.EMAIL_API_KEY or not config.EMAIL_FROM:
            raise EmailNotConfigured(
                "Почтовый сервис не настроен — пропиши VERF_EMAIL_API_KEY и VERF_EMAIL_FROM в .env"
            )
        (_send_brevo if provider == "brevo" else _send_unisender)(to, subject, html_body)
        return
    if provider != "smtp":
        raise EmailNotConfigured(f"Неизвестный VERF_EMAIL_PROVIDER: {provider}")

    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        raise EmailNotConfigured(
            "SMTP не настроен — пропиши VERF_SMTP_USER и VERF_SMTP_PASSWORD в .env "
            "(для Yandex: Почта → Настройки → Пароли приложений)"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, [to], msg.as_string())
    except smtplib.SMTPException as exc:
        raise EmailError(f"Не удалось отправить письмо: {exc}")
    except OSError as exc:
        raise EmailError(f"Не удалось подключиться к SMTP-серверу: {exc}")


def _brand_wrapper(inner_html: str) -> str:
    """Minimal shared wrapper so every email looks like it's from the same
    product without duplicating the boilerplate in each template."""
    return f"""
    <div style="font-family: 'IBM Plex Sans', Arial, sans-serif; max-width: 480px; margin: 0 auto; color: #0F2138;">
      <div style="padding: 24px 0 16px; border-bottom: 1px solid #B9C6D6; margin-bottom: 24px;">
        <span style="font-family: Arial, sans-serif; font-weight: 700; font-size: 18px;">VERF</span>
      </div>
      {inner_html}
      <div style="margin-top: 32px; padding-top: 16px; border-top: 1px solid #B9C6D6; font-size: 12px; color: #48607A;">
        VERF · verfdeploy.ru
      </div>
    </div>
    """


def send_welcome_email(to: str) -> None:
    html = _brand_wrapper("""
      <h2 style="font-size: 20px; margin-bottom: 12px;">Добро пожаловать в VERF</h2>
      <p style="font-size: 14px; line-height: 1.6;">
        Аккаунт создан. Заходи в личный кабинет — там можно подключить GitHub,
        загрузить проект архивом или через CLI, и развернуть его за пару минут.
      </p>
      <a href="https://cabinet.verfdeploy.ru" style="display: inline-block; margin-top: 16px; padding: 10px 20px;
         background: #0F2138; color: #EDF2F6; text-decoration: none; border-radius: 4px; font-size: 14px;">
        Открыть кабинет
      </a>
    """)
    send_email(to, "Добро пожаловать в VERF", html)


def send_payment_confirmation_email(to: str, plan: str, amount_rub: int, expires_at=None) -> None:
    plan_label = {"pro": "Pro", "business": "Business"}.get(plan, plan)
    until = f" Тариф включён до <b>{expires_at.strftime('%d.%m.%Y')}</b>." if expires_at else ""
    html = _brand_wrapper(f"""
      <h2 style="font-size: 20px; margin-bottom: 12px;">Оплата прошла успешно</h2>
      <p style="font-size: 14px; line-height: 1.6;">
        Тариф <b>{plan_label}</b> активирован — списано {amount_rub}₽.{until}
        Историю платежей можно посмотреть в личном кабинете, раздел «Тариф».
      </p>
      <a href="https://cabinet.verfdeploy.ru" style="display: inline-block; margin-top: 16px; padding: 10px 20px;
         background: #0F2138; color: #EDF2F6; text-decoration: none; border-radius: 4px; font-size: 14px;">
        Открыть кабинет
      </a>
    """)
    send_email(to, f"Тариф {plan_label} активирован", html)
