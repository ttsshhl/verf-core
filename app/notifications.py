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


_BTN = ('style="display: inline-block; margin-top: 16px; padding: 10px 20px; background: #0F2138; '
        'color: #EDF2F6; text-decoration: none; border-radius: 4px; font-size: 14px;"')


def _plan_label(plan: str) -> str:
    return {"pro": "Pro", "business": "Business", "free": "Free"}.get(plan, plan)


def send_subscription_expiring_email(to: str, plan: str, expires_at) -> None:
    html = _brand_wrapper(f"""
      <h2 style="font-size: 20px; margin-bottom: 12px;">Тариф заканчивается</h2>
      <p style="font-size: 14px; line-height: 1.6;">
        Тариф <b>{_plan_label(plan)}</b> действует до <b>{expires_at.strftime('%d.%m.%Y')}</b>. Автоматического
        списания нет — чтобы не потерять лимиты и дополнительные проекты, продли тариф в личном кабинете
        заранее: оставшиеся дни не сгорят, новый месяц добавится к ним.
      </p>
      <a href="https://cabinet.verfdeploy.ru" {_BTN}>Продлить тариф</a>
    """)
    send_email(to, f"Тариф {_plan_label(plan)} заканчивается {expires_at.strftime('%d.%m')}", html)


def send_subscription_expired_email(to: str, plan: str, paused_slugs: list[str] | None = None) -> None:
    paused = ""
    if paused_slugs:
        names = ", ".join(paused_slugs)
        paused = (f"<p style=\"font-size: 14px; line-height: 1.6;\">На тарифе Free доступен один проект, поэтому "
                  f"остановлены: <b>{names}</b>. Ничего не удалено — при оплате тарифа они запустятся снова.</p>")
    html = _brand_wrapper(f"""
      <h2 style="font-size: 20px; margin-bottom: 12px;">Тариф закончился</h2>
      <p style="font-size: 14px; line-height: 1.6;">
        Срок тарифа <b>{_plan_label(plan)}</b> истёк, аккаунт переведён на бесплатный тариф Free
        (256 МБ памяти на проект, один проект). Твои данные и проекты на месте.
      </p>
      {paused}
      <a href="https://cabinet.verfdeploy.ru" {_BTN}>Вернуть тариф</a>
    """)
    send_email(to, f"Тариф {_plan_label(plan)} закончился", html)


def send_payment_canceled_email(to: str, plan: str) -> None:
    html = _brand_wrapper(f"""
      <h2 style="font-size: 20px; margin-bottom: 12px;">Платёж не прошёл</h2>
      <p style="font-size: 14px; line-height: 1.6;">
        Оплата тарифа <b>{_plan_label(plan)}</b> не завершилась — банк или платёжная система отклонила платёж,
        либо он был отменён. Деньги не списаны (если они всё же ушли со счёта, вернутся автоматически в течение нескольких дней).
        Попробуй ещё раз другим способом — картой или через СБП.
      </p>
      <a href="https://cabinet.verfdeploy.ru" {_BTN}>Повторить оплату</a>
    """)
    send_email(to, "Платёж не прошёл", html)


def send_password_reset_email(to: str, link: str, ttl_minutes: int) -> None:
    html = _brand_wrapper(f"""
      <h2 style="font-size: 20px; margin-bottom: 12px;">Сброс пароля</h2>
      <p style="font-size: 14px; line-height: 1.6;">
        Мы получили запрос на смену пароля в VERF. Ссылка действует {ttl_minutes} минут и срабатывает один раз.
        Если это были не вы — просто проигнорируйте письмо, пароль останется прежним.
      </p>
      <a href="{link}" {_BTN}>Задать новый пароль</a>
    """)
    send_email(to, "Сброс пароля VERF", html)


def send_project_crashed_email(to: str, slug: str, log_tail: str, hint: str | None) -> None:
    import html as _html
    hint_html = f'<p style="font-size: 14px; line-height: 1.6;"><b>Что делать:</b> {_html.escape(hint)}</p>' if hint else ""
    tail_html = (f'<pre style="background:#F2F5F8; padding:12px; border-radius:4px; font-size:12px; overflow-x:auto; white-space:pre-wrap;">'
                 f'{_html.escape(log_tail[-1500:])}</pre>') if log_tail else ""
    page = _brand_wrapper(f"""
      <h2 style="font-size: 20px; margin-bottom: 12px;">Проект «{_html.escape(slug)}» остановлен</h2>
      <p style="font-size: 14px; line-height: 1.6;">
        Проект постоянно падал при запуске, поэтому мы остановили его, чтобы он не перезапускался по кругу и не
        нагружал сервер. Ничего не удалено.
      </p>
      {hint_html}
      <p style="font-size: 13px; color:#555;">Последние строки лога:</p>
      {tail_html}
      <p style="font-size: 14px; line-height: 1.6;">Исправьте причину и запустите проект заново в личном кабинете (кнопка перезапуска или новый деплой).</p>
      <a href="https://cabinet.verfdeploy.ru" {_BTN}>Открыть кабинет</a>
    """)
    send_email(to, f"Проект {slug} остановлен: не удаётся запуститься", page)
