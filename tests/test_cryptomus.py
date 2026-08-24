from app import billing


def test_signature_is_deterministic(monkeypatch):
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "test-api-key")
    data = {"amount": "15", "currency": "USD", "order_id": "sub-1"}
    sig1 = billing._cryptomus_sign(data)
    sig2 = billing._cryptomus_sign(data)
    assert sig1 == sig2
    assert len(sig1) == 32  # MD5 hex digest length


def test_signature_changes_with_different_key(monkeypatch):
    data = {"amount": "15", "currency": "USD", "order_id": "sub-1"}
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "key-a")
    sig_a = billing._cryptomus_sign(data)
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "key-b")
    sig_b = billing._cryptomus_sign(data)
    assert sig_a != sig_b


def test_signature_changes_with_different_data(monkeypatch):
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "test-api-key")
    sig1 = billing._cryptomus_sign({"amount": "15", "order_id": "sub-1"})
    sig2 = billing._cryptomus_sign({"amount": "16", "order_id": "sub-1"})
    assert sig1 != sig2


def test_verify_webhook_accepts_correctly_signed_payload(monkeypatch):
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "test-api-key")
    data = {"uuid": "pay-123", "status": "paid", "order_id": "sub-1"}
    sig = billing._cryptomus_sign(data)
    body = {**data, "sign": sig}
    assert billing.verify_cryptomus_signature(body) is True


def test_verify_webhook_rejects_tampered_status(monkeypatch):
    """The core security property: an attacker who intercepts a real webhook
    and flips status from 'cancel' to 'paid' invalidates the signature,
    because the signature covers the whole body, not just some fields.
    """
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "test-api-key")
    original = {"uuid": "pay-123", "status": "cancel", "order_id": "sub-1"}
    sig = billing._cryptomus_sign(original)

    tampered = {"uuid": "pay-123", "status": "paid", "order_id": "sub-1", "sign": sig}
    assert billing.verify_cryptomus_signature(tampered) is False


def test_verify_webhook_rejects_missing_sign(monkeypatch):
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "test-api-key")
    assert billing.verify_cryptomus_signature({"uuid": "pay-123", "status": "paid"}) is False


def test_verify_webhook_rejects_when_key_not_configured(monkeypatch):
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "")
    body = {"uuid": "pay-123", "status": "paid", "sign": "whatever"}
    assert billing.verify_cryptomus_signature(body) is False


def test_create_crypto_payment_requires_credentials(monkeypatch):
    monkeypatch.setattr(billing, "CRYPTOMUS_MERCHANT_ID", "")
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "")
    import pytest
    with pytest.raises(billing.BillingError):
        billing.create_crypto_payment("pro", "sub-1")


def test_create_crypto_payment_rejects_unknown_plan(monkeypatch):
    monkeypatch.setattr(billing, "CRYPTOMUS_MERCHANT_ID", "m")
    monkeypatch.setattr(billing, "CRYPTOMUS_API_KEY", "k")
    import pytest
    with pytest.raises(billing.BillingError):
        billing.create_crypto_payment("ultra-plan", "sub-1")
