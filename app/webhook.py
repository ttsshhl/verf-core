"""GitHub webhook signature verification (HMAC-SHA256, X-Hub-Signature-256).

Pure function, no I/O — easy to unit test and easy to reason about, since
this is the one thing that must never be wrong: a broken check here means
anyone on the internet can trigger a deploy on your infrastructure.
"""
import hashlib
import hmac


def verify_signature(payload_body: bytes, secret: str, signature_header: str | None) -> bool:
    if not secret:
        # Explicit opt-out for local dev only — config.py logs a warning if this
        # path is reachable in a real deployment (no secret configured).
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def extract_push_info(payload: dict) -> tuple[str, str]:
    """Return (branch, commit_sha) from a GitHub push event payload."""
    ref = payload.get("ref", "")  # e.g. "refs/heads/main" or "refs/heads/feature/x"
    prefix = "refs/heads/"
    branch = ref[len(prefix):] if ref.startswith(prefix) else ref
    commit_sha = payload.get("after", "")
    return branch, commit_sha
