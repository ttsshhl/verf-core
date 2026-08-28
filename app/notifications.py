"""Transactional email — welcome on registration, confirmation on payment.

Sends over SMTP (Yandex Mail by default). Every call site treats sending as
best-effort: a broken SMTP config or a transient network hiccup must never
block registration or payment activation, so callers always run this via
FastAPI's BackgroundTasks and swallow EmailError rather than propagating it.
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_EMAIL, SMTP_FROM_NAME


class EmailError(Exception):
    pass


def send_email(to: str, subject: str, html_body: str) -> None:
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        raise EmailError(
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


def send_payment_confirmation_email(to: str, plan: str, amount_rub: int) -> None:
    plan_label = {"pro": "Pro", "business": "Business"}.get(plan, plan)
    html = _brand_wrapper(f"""
      <h2 style="font-size: 20px; margin-bottom: 12px;">Оплата прошла успешно</h2>
      <p style="font-size: 14px; line-height: 1.6;">
        Тариф <b>{plan_label}</b> активирован — списано {amount_rub}₽. Подписка продлится автоматически
        через 30 дней, отменить можно в любой момент в личном кабинете.
      </p>
      <a href="https://cabinet.verfdeploy.ru" style="display: inline-block; margin-top: 16px; padding: 10px 20px;
         background: #0F2138; color: #EDF2F6; text-decoration: none; border-radius: 4px; font-size: 14px;">
        Открыть кабинет
      </a>
    """)
    send_email(to, f"Тариф {plan_label} активирован", html)
