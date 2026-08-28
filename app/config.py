import os
from pathlib import Path

# --- Core paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = Path(os.getenv("VERF_WORKSPACE_DIR", BASE_DIR / "workspace"))
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# Max size for a ZIP archive uploaded via the CLI or the cabinet's drag-drop
# deploy — generous enough for a real small project's source (not node_modules
# or venv, which .verfignore/.gitignore-style filtering keeps out client-side).
MAX_UPLOAD_SIZE_MB = int(os.getenv("VERF_MAX_UPLOAD_SIZE_MB", "50"))

# --- Database ---
DATABASE_URL = os.getenv("VERF_DATABASE_URL", f"sqlite:///{BASE_DIR / 'verf.db'}")

# --- GitHub webhook ---
# Secret configured in the GitHub repo webhook settings. Required in production;
# empty string only allowed to make local testing without a real webhook easier.
GITHUB_WEBHOOK_SECRET = os.getenv("VERF_GITHUB_WEBHOOK_SECRET", "")

# --- Docker / deploy ---
DOCKER_NETWORK = os.getenv("VERF_DOCKER_NETWORK", "verf-net")
DOMAIN_SUFFIX = os.getenv("VERF_DOMAIN_SUFFIX", "verf.dev")
TRAEFIK_ENTRYPOINT = os.getenv("VERF_TRAEFIK_ENTRYPOINT", "websecure")
TRAEFIK_CERTRESOLVER = os.getenv("VERF_TRAEFIK_CERTRESOLVER", "le")

# Default resource limits per container (MVP / Free tier). Overridable per project later.
# Per-container resource limits, now actually tiered by the project owner's plan
# (previously every project got the same "free" limit regardless of tariff —
# closing that gap: this is what the landing page has promised all along).
PLAN_MEM_LIMITS = {"free": "512m", "pro": "2048m", "business": "8192m"}
PLAN_CPU_QUOTAS = {"free": 50_000, "pro": 100_000, "business": 200_000}  # cpu-period=100000, so 0.5 / 1.0 / 2.0 vCPU
# Fallback for containers with no resolvable owner plan (e.g. admin-created projects).
DEFAULT_MEM_LIMIT = os.getenv("VERF_DEFAULT_MEM_LIMIT", PLAN_MEM_LIMITS["free"])
DEFAULT_CPU_QUOTA = int(os.getenv("VERF_DEFAULT_CPU_QUOTA", str(PLAN_CPU_QUOTAS["free"])))

# --- Control plane auth ---
# Single admin key for MVP (register/delete any project). Swap for real user auth later.
ADMIN_API_KEY = os.getenv("VERF_ADMIN_API_KEY", "")

# --- User auth (personal cabinet) ---
JWT_SECRET = os.getenv("VERF_JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("VERF_JWT_EXPIRE_MINUTES", str(60 * 24 * 30)))  # 30 days

# --- Billing plans ---
# None = unlimited projects for that plan.
PLAN_PROJECT_LIMITS = {"free": 1, "pro": 5, "business": None}
PLAN_PRICES_RUB = {"pro": 490, "business": 1990}  # launch pricing — see README for rationale

# --- ЮKassa (payment provider) ---
YOOKASSA_SHOP_ID = os.getenv("VERF_YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("VERF_YOOKASSA_SECRET_KEY", "")
YOOKASSA_RETURN_URL = os.getenv("VERF_YOOKASSA_RETURN_URL", f"https://{DOMAIN_SUFFIX}/billing/thanks")

# --- Cryptomus (crypto payment provider) ---
CRYPTOMUS_MERCHANT_ID = os.getenv("VERF_CRYPTOMUS_MERCHANT_ID", "")
CRYPTOMUS_API_KEY = os.getenv("VERF_CRYPTOMUS_API_KEY", "")
CRYPTOMUS_CALLBACK_URL = os.getenv("VERF_CRYPTOMUS_CALLBACK_URL", f"https://api.{DOMAIN_SUFFIX}/webhook/cryptomus")
# Rough USD peg for RUB prices, since Cryptomus invoices are priced in USD/USDT.
# This is a fixed approximation, not a live exchange rate — adjust as the rate moves,
# or wire up a live-rate lookup later if the spread starts to matter.
PLAN_PRICES_USD = {"pro": 6, "business": 25}

# --- GitHub OAuth (connect account, list repos, auto-create webhooks) ---
# Create an OAuth App at https://github.com/settings/developers — Authorization
# callback URL must exactly match GITHUB_REDIRECT_URI below.
GITHUB_CLIENT_ID = os.getenv("VERF_GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("VERF_GITHUB_CLIENT_SECRET", "")
GITHUB_REDIRECT_URI = os.getenv("VERF_GITHUB_REDIRECT_URI", f"https://api.{DOMAIN_SUFFIX}/auth/github/callback")
GITHUB_CONNECT_NONCE_TTL_SECONDS = 120

# --- Email (transactional — welcome + payment confirmation) ---
# Yandex Mail SMTP by default, since that's what the business already uses.
# For Yandex specifically: Почта → Настройки → Пароли приложений → создать
# отдельный пароль для SMTP (не аккаунтный пароль — Yandex requires an
# app-specific password for SMTP/IMAP access).
SMTP_HOST = os.getenv("VERF_SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("VERF_SMTP_PORT", "465"))
SMTP_USER = os.getenv("VERF_SMTP_USER", "")
SMTP_PASSWORD = os.getenv("VERF_SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("VERF_SMTP_FROM_EMAIL", SMTP_USER)
SMTP_FROM_NAME = os.getenv("VERF_SMTP_FROM_NAME", "VERF")
