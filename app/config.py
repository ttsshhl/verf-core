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
# Single admin key for MVP (register/delete projects). Swap for real user auth later.
ADMIN_API_KEY = os.getenv("VERF_ADMIN_API_KEY", "")
