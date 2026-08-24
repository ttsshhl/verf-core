import os
from pathlib import Path

# --- Core paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = Path(os.getenv("VERF_WORKSPACE_DIR", BASE_DIR / "workspace"))
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

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
DEFAULT_MEM_LIMIT = os.getenv("VERF_DEFAULT_MEM_LIMIT", "512m")
DEFAULT_CPU_QUOTA = int(os.getenv("VERF_DEFAULT_CPU_QUOTA", str(50_000)))  # 0.5 CPU (cpu-period=100000 default)

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
PLAN_PRICES_RUB = {"pro": 990, "business": 2990}  # monthly, matches the landing page

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
PLAN_PRICES_USD = {"pro": 12, "business": 35}
