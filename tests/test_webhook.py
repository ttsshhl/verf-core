import hashlib
import hmac

from app.webhook import verify_signature, extract_push_info


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_accepted():
    secret = "s3cr3t"
    body = b'{"ref": "refs/heads/main"}'
    sig = _sign(secret, body)
    assert verify_signature(body, secret, sig) is True


def test_wrong_secret_rejected():
    body = b'{"ref": "refs/heads/main"}'
    sig = _sign("right-secret", body)
    assert verify_signature(body, "wrong-secret", sig) is False


def test_tampered_body_rejected():
    secret = "s3cr3t"
    sig = _sign(secret, b'{"ref": "refs/heads/main"}')
    tampered_body = b'{"ref": "refs/heads/main", "evil": true}'
    assert verify_signature(tampered_body, secret, sig) is False


def test_missing_header_rejected_when_secret_set():
    assert verify_signature(b"body", "s3cr3t", None) is False


def test_malformed_header_rejected():
    assert verify_signature(b"body", "s3cr3t", "not-sha256-prefixed") is False


def test_no_secret_configured_allows_through():
    # Explicit local-dev escape hatch
    assert verify_signature(b"anything", "", None) is True


def test_extract_push_info():
    payload = {"ref": "refs/heads/main", "after": "abc123def456"}
    branch, sha = extract_push_info(payload)
    assert branch == "main"
    assert sha == "abc123def456"


def test_extract_push_info_feature_branch():
    payload = {"ref": "refs/heads/feature/x", "after": "deadbeef"}
    branch, sha = extract_push_info(payload)
    assert branch == "feature/x"
