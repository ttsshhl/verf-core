"""ЮKassa + Cryptomus integrations.

ЮKassa security note: webhook payloads are NOT trusted directly. On webhook
receipt we fetch the payment by ID from the ЮKassa API ourselves
(authenticated with our own shop credentials) and only act on what THAT
response says. This is the standard, safe pattern — an attacker can POST a
fake "succeeded" webhook body, but they can't make ЮKassa's own API return
a fake payment status.

Cryptomus security note: Cryptomus signs every webhook with your API key
(MD5 of the base64-encoded body concatenated with the key — this is their
documented scheme, see doc.cryptomus.com/merchant-api/payments/webhook).
Unlike ЮKassa, this signature IS cryptographically meaningful — a forged
body without knowing your API key won't produce a matching signature — so
a valid signature is sufficient to trust the payload's status field.
"""
import base64
import hashlib
import hmac
import json
import uuid

import requests

from app.config import (
    YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_RETURN_URL, PLAN_PRICES_RUB,
    CRYPTOMUS_MERCHANT_ID, CRYPTOMUS_API_KEY, CRYPTOMUS_CALLBACK_URL, PLAN_PRICES_USD,
)

YOOKASSA_API = "https://api.yookassa.ru/v3"
CRYPTOMUS_API = "https://api.cryptomus.com/v1"


class BillingError(Exception):
    pass


def _auth():
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise BillingError(
            "ЮKassa не настроена: пропиши VERF_YOOKASSA_SHOP_ID и VERF_YOOKASSA_SECRET_KEY в .env "
            "(получить в личном кабинете ЮKassa после регистрации магазина)"
        )
    return (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)


def create_payment(plan: str, subscription_id: str) -> dict:
    """Creates a payment in ЮKassa and returns its data, including the
    confirmation URL the user is redirected to for entering card details.
    """
    if plan not in PLAN_PRICES_RUB:
        raise BillingError(f"Неизвестный тариф: {plan}")

    amount = PLAN_PRICES_RUB[plan]
    idempotence_key = str(uuid.uuid4())

    response = requests.post(
        f"{YOOKASSA_API}/payments",
        auth=_auth(),
        headers={"Idempotence-Key": idempotence_key},
        json={
            "amount": {"value": f"{amount}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
            "capture": True,
            "description": f"VERF — тариф {plan} (подписка {subscription_id})",
            "metadata": {"subscription_id": subscription_id, "plan": plan},
        },
        timeout=15,
    )
    if response.status_code >= 300:
        raise BillingError(f"ЮKassa вернула ошибку {response.status_code}: {response.text}")
    return response.json()


def fetch_payment(payment_id: str) -> dict:
    """Authoritative source of truth for a payment's real status."""
    response = requests.get(f"{YOOKASSA_API}/payments/{payment_id}", auth=_auth(), timeout=15)
    if response.status_code >= 300:
        raise BillingError(f"ЮKassa вернула ошибку {response.status_code}: {response.text}")
    return response.json()


def plan_project_limit(plan: str) -> int | None:
    from app.config import PLAN_PROJECT_LIMITS
    return PLAN_PROJECT_LIMITS.get(plan, PLAN_PROJECT_LIMITS["free"])


# ---------- Cryptomus ----------

def _cryptomus_auth():
    if not CRYPTOMUS_MERCHANT_ID or not CRYPTOMUS_API_KEY:
        raise BillingError(
            "Cryptomus не настроен: пропиши VERF_CRYPTOMUS_MERCHANT_ID и VERF_CRYPTOMUS_API_KEY в .env "
            "(получить после регистрации на cryptomus.com и создания проекта)"
        )


def _cryptomus_sign(data: dict) -> str:
    """MD5(base64(json_no_spaces_unicode_preserved) + api_key) — Cryptomus's documented scheme."""
    encoded = base64.b64encode(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    )
    return hashlib.md5(encoded + CRYPTOMUS_API_KEY.encode()).hexdigest()


def create_crypto_payment(plan: str, subscription_id: str) -> dict:
    """Creates a Cryptomus invoice and returns its data, including the
    payment page URL the user is redirected to for choosing coin/network.
    """
    if plan not in PLAN_PRICES_USD:
        raise BillingError(f"Неизвестный тариф: {plan}")
    _cryptomus_auth()

    amount = PLAN_PRICES_USD[plan]
    data = {
        "amount": str(amount),
        "currency": "USD",
        "order_id": subscription_id,
        "url_callback": CRYPTOMUS_CALLBACK_URL,
    }
    response = requests.post(
        f"{CRYPTOMUS_API}/payment",
        headers={
            "merchant": CRYPTOMUS_MERCHANT_ID,
            "sign": _cryptomus_sign(data),
            "Content-Type": "application/json",
        },
        json=data,
        timeout=15,
    )
    if response.status_code >= 300:
        raise BillingError(f"Cryptomus вернула ошибку {response.status_code}: {response.text}")
    body = response.json()
    if body.get("state") != 0:  # Cryptomus convention: 0 = success
        raise BillingError(f"Cryptomus отклонила запрос: {body}")
    return body["result"]


def verify_cryptomus_signature(body: dict) -> bool:
    """Checks the `sign` field Cryptomus attaches to every webhook payload.

    A missing/invalid signature means the request either isn't really from
    Cryptomus or was tampered with in transit — reject it either way.
    """
    if not CRYPTOMUS_API_KEY:
        return False
    data = dict(body)
    received_sign = data.pop("sign", None)
    if not received_sign:
        return False
    expected = _cryptomus_sign(data)
    return hmac.compare_digest(expected, received_sign)
