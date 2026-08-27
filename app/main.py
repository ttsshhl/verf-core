import json
import re
import secrets
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Depends, HTTPException, Request, BackgroundTasks, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import auth, billing, github as gh
from app.config import (
    ADMIN_API_KEY, DOMAIN_SUFFIX, PLAN_PROJECT_LIMITS, GITHUB_CONNECT_NONCE_TTL_SECONDS,
    MAX_UPLOAD_SIZE_MB, WORKSPACE_DIR,
)
from app.db import get_db, init_db
from app.models import Project, Deployment, DeployStatus, User, Subscription, SubscriptionStatus
from app.pipeline import run_deploy, run_deploy_from_upload
from app.schemas import (
    ProjectCreate, ProjectOut, DeploymentOut,
    UserCreate, UserLogin, UserOut, TokenOut, SubscribeRequest, SubscriptionOut, GithubRepoOut,
    DomainRequest,
)
from app.webhook import verify_signature, extract_push_info


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="VERF core", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://{DOMAIN_SUFFIX}", f"https://cabinet.{DOMAIN_SUFFIX}", f"https://www.{DOMAIN_SUFFIX}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single-use nonces for starting the GitHub OAuth "connect" flow. A browser
# full-page navigation can't carry an Authorization header, so the cabinet
# first fetches a nonce via a normal authenticated request, then navigates
# to /auth/github/start?nonce=... — keeping the user's JWT out of the URL
# (and therefore out of server access logs). In-memory is fine for MVP:
# short TTL, single verf-core process.
_GITHUB_CONNECT_NONCES: dict[str, tuple[str, float]] = {}


def require_admin(x_api_key: str | None = Header(default=None)):
    if not ADMIN_API_KEY:
        return  # local dev — no admin key configured
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий X-API-Key")


def get_current_user(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Требуется авторизация: заголовок Authorization: Bearer <token>")
    token = authorization.removeprefix("Bearer ")
    try:
        user_id = auth.decode_access_token(token)
    except auth.InvalidToken as exc:
        raise HTTPException(status_code=401, detail=f"Недействительный токен: {exc}")
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user


@app.get("/health")
def health():
    return {"status": "ok"}


# Cached lookup of the server's own current public IP — used to show users
# accurate DNS instructions (apex-domain A record) without hardcoding an IP
# anywhere in the frontend, which would go stale the next time the server's
# IP changes (has happened multiple times already due to upstream blocking).
_server_ip_cache = {"ip": None, "fetched_at": 0.0}
_SERVER_IP_CACHE_TTL = 3600  # seconds


@app.get("/server-info")
def server_info():
    now = time.time()
    if not _server_ip_cache["ip"] or now - _server_ip_cache["fetched_at"] > _SERVER_IP_CACHE_TTL:
        try:
            import requests
            resp = requests.get("https://api.ipify.org?format=json", timeout=5)
            _server_ip_cache["ip"] = resp.json()["ip"]
            _server_ip_cache["fetched_at"] = now
        except Exception:
            pass  # keep the last known-good value (or None if we've never succeeded)
    return {"ip": _server_ip_cache["ip"], "domain_suffix": DOMAIN_SUFFIX}


# ---------- Auth ----------

@app.post("/auth/register", response_model=TokenOut)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter_by(email=payload.email).first():
        raise HTTPException(status_code=409, detail="Пользователь с такой почтой уже зарегистрирован")
    user = User(email=payload.email, password_hash=auth.hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenOut(access_token=auth.create_access_token(user.id))


@app.post("/auth/login", response_model=TokenOut)
def login(payload: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=payload.email).first()
    if not user or not auth.verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверная почта или пароль")
    return TokenOut(access_token=auth.create_access_token(user.id))


@app.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


# ---------- GitHub OAuth (connect account, list repos) ----------

@app.post("/auth/github/prepare-connect")
def prepare_github_connect(user: User = Depends(get_current_user)):
    nonce = secrets.token_urlsafe(24)
    _GITHUB_CONNECT_NONCES[nonce] = (user.id, time.time() + GITHUB_CONNECT_NONCE_TTL_SECONDS)
    return {"nonce": nonce}


@app.get("/auth/github/start")
def start_github_connect(nonce: str):
    entry = _GITHUB_CONNECT_NONCES.pop(nonce, None)
    if not entry or entry[1] < time.time():
        raise HTTPException(status_code=400, detail="Ссылка для подключения GitHub устарела — начни заново")
    user_id, _ = entry
    try:
        state = auth.create_github_connect_state(user_id)
        url = gh.authorize_url(state)
    except gh.GithubError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return RedirectResponse(url)


@app.get("/auth/github/callback")
def github_connect_callback(code: str, state: str, db: Session = Depends(get_db)):
    try:
        user_id = auth.decode_github_connect_state(state)
    except auth.InvalidToken:
        raise HTTPException(status_code=400, detail="Недействительное состояние OAuth — начни подключение заново")

    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    try:
        token = gh.exchange_code(code)
        username = gh.fetch_username(token)
    except gh.GithubError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    user.github_token = token
    user.github_username = username
    db.commit()

    return RedirectResponse(f"https://cabinet.{DOMAIN_SUFFIX}?github=connected")


@app.delete("/me/github")
def disconnect_github(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.github_token = None
    user.github_username = None
    db.commit()
    return {"disconnected": True}


@app.get("/me/github/repos", response_model=list[GithubRepoOut])
def list_my_github_repos(user: User = Depends(get_current_user)):
    if not user.github_token:
        raise HTTPException(status_code=400, detail="GitHub не подключён — сначала подключи аккаунт")
    try:
        repos = gh.list_repos(user.github_token)
    except gh.GithubError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return [
        GithubRepoOut(
            name=r["name"], full_name=r["full_name"],
            default_branch=r.get("default_branch", "main"), private=r.get("private", False),
        )
        for r in repos
    ]


# ---------- Personal cabinet: self-service projects ----------

@app.post("/me/projects", response_model=ProjectOut)
def create_my_project(payload: ProjectCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if db.query(Project).filter_by(slug=payload.slug).first():
        raise HTTPException(status_code=409, detail="Проект с таким slug уже существует")

    limit = PLAN_PROJECT_LIMITS.get(user.plan, PLAN_PROJECT_LIMITS["free"])
    if limit is not None:
        current_count = db.query(Project).filter_by(owner_id=user.id).count()
        if current_count >= limit:
            raise HTTPException(
                status_code=402,
                detail=f"Лимит тарифа «{user.plan}» — {limit} проект(ов). Оформи более высокий тариф в /billing/subscribe.",
            )

    repo_url = payload.repo_url
    if payload.repo_full_name:
        repo_url = f"https://github.com/{payload.repo_full_name}.git"

    project = Project(
        owner_id=user.id, slug=payload.slug, repo_url=repo_url, branch=payload.branch,
        kind=payload.kind, env_json=json.dumps(payload.env),
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    # Best-effort: if the project came from the GitHub picker and the user has
    # a connected account, create the push webhook for them automatically.
    # Failure here is never fatal to project creation — the user can still
    # fall back to configuring the webhook by hand with the secret below.
    if payload.repo_full_name and user.github_token:
        payload_url = f"https://api.{DOMAIN_SUFFIX}/webhook/github/{project.slug}"
        if gh.create_webhook(user.github_token, payload.repo_full_name, payload_url, project.webhook_secret):
            project.webhook_auto_configured = True
            db.commit()

    return _to_project_out(project)


@app.get("/me/projects", response_model=list[ProjectOut])
def list_my_projects(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [_to_project_out(p) for p in db.query(Project).filter_by(owner_id=user.id).all()]


@app.get("/me/projects/{slug}/deployments", response_model=list[DeploymentOut])
def list_my_deployments(slug: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = db.query(Project).filter_by(slug=slug, owner_id=user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден или принадлежит другому пользователю")
    return [_to_deployment_out(d) for d in project.deployments]


@app.post("/me/projects/{slug}/deploy", response_model=DeploymentOut)
async def deploy_from_archive(
    slug: str, background_tasks: BackgroundTasks,
    archive: UploadFile = File(...),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """Deploy path for the CLI (`verf deploy`) and the cabinet's drag-drop
    upload — no git involved. Accepts a ZIP of the project, saves it to a
    temp path, and runs the same build+run pipeline as the GitHub webhook
    (just entering through builder.replace_from_archive instead of a clone).
    """
    project = db.query(Project).filter_by(slug=slug, owner_id=user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден или принадлежит другому пользователю")

    if not archive.filename or not archive.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Ожидается ZIP-архив")

    max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024
    body = await archive.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Архив больше {MAX_UPLOAD_SIZE_MB} МБ")
    if len(body) == 0:
        raise HTTPException(status_code=400, detail="Пустой файл")

    upload_dir = WORKSPACE_DIR / ".uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"{project.slug}-{uuid.uuid4().hex[:12]}.zip"
    saved_path.write_bytes(body)

    deployment = Deployment(project_id=project.id, status=DeployStatus.pending)
    db.add(deployment)
    db.commit()
    db.refresh(deployment)

    background_tasks.add_task(run_deploy_from_upload, db, project, deployment, saved_path)

    return _to_deployment_out(deployment)


@app.get("/me/deployments/{deployment_id}", response_model=DeploymentOut)
def get_my_deployment(deployment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Lets the CLI poll a single deployment's status/log until it's terminal
    (running/failed), without fetching the whole project's history each time."""
    deployment = db.query(Deployment).filter_by(id=deployment_id).first()
    if not deployment or not deployment.project or deployment.project.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Деплой не найден")
    return _to_deployment_out(deployment)


@app.post("/me/projects/{slug}/domain", response_model=ProjectOut)
def set_custom_domain(
    slug: str, payload: DomainRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    project = db.query(Project).filter_by(slug=slug, owner_id=user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден или принадлежит другому пользователю")

    domain = _validate_domain(payload.domain)

    existing = db.query(Project).filter(Project.custom_domain == domain, Project.id != project.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Этот домен уже привязан к другому проекту")

    project.custom_domain = domain
    db.commit()
    _try_apply_domain_to_running_container(db, project)
    db.refresh(project)
    return _to_project_out(project)


@app.delete("/me/projects/{slug}/domain", response_model=ProjectOut)
def remove_custom_domain(slug: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = db.query(Project).filter_by(slug=slug, owner_id=user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден или принадлежит другому пользователю")

    project.custom_domain = None
    db.commit()
    _try_apply_domain_to_running_container(db, project)
    db.refresh(project)
    return _to_project_out(project)


def _try_apply_domain_to_running_container(db: Session, project: Project) -> None:
    """Best-effort: if the project has a currently-running deployment,
    recreate its container right away so the domain change (add/remove)
    takes effect immediately instead of waiting for the next push. If
    Docker isn't reachable or anything else goes wrong, the domain is still
    saved in the database — it'll simply apply on the next real deploy.
    """
    latest_running = (
        db.query(Deployment)
        .filter_by(project_id=project.id, status=DeployStatus.running)
        .order_by(Deployment.created_at.desc())
        .first()
    )
    if not latest_running or not latest_running.image_tag:
        return

    from app import deployer
    from app.config import PLAN_MEM_LIMITS, PLAN_CPU_QUOTAS, DEFAULT_MEM_LIMIT, DEFAULT_CPU_QUOTA
    owner_plan = project.owner.plan if project.owner else "free"
    mem_limit = PLAN_MEM_LIMITS.get(owner_plan, DEFAULT_MEM_LIMIT)
    cpu_quota = PLAN_CPU_QUOTAS.get(owner_plan, DEFAULT_CPU_QUOTA)
    env = json.loads(project.env_json or "{}")

    try:
        deployer.run_container(
            project.slug, latest_running.image_tag, latest_running.port, env,
            mem_limit=mem_limit, cpu_quota=cpu_quota, custom_domain=project.custom_domain,
        )
    except deployer.DeployError:
        pass  # best-effort — domain is saved regardless, will apply on next real deploy


@app.delete("/me/projects/{slug}")
def delete_my_project(slug: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = db.query(Project).filter_by(slug=slug, owner_id=user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден или принадлежит другому пользователю")
    from app import deployer
    deployer.stop_and_remove(project.slug)
    db.delete(project)
    db.commit()
    return {"deleted": slug}


# ---------- Billing ----------

@app.post("/billing/subscribe", response_model=SubscriptionOut)
def subscribe(payload: SubscribeRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.config import PLAN_PRICES_RUB
    if payload.plan not in PLAN_PRICES_RUB:
        raise HTTPException(status_code=400, detail="Тариф должен быть 'pro' или 'business'")
    if payload.provider not in ("yookassa", "sbp", "cryptomus"):
        raise HTTPException(status_code=400, detail="provider должен быть 'yookassa', 'sbp' или 'cryptomus'")

    # "sbp" is still the ЮKassa gateway underneath (same account, same webhook) —
    # it only pre-selects SBP as the payment method instead of card. Stored as
    # "yookassa" so /webhook/yookassa's provider filter still matches it.
    gateway = "cryptomus" if payload.provider == "cryptomus" else "yookassa"

    subscription = Subscription(
        user_id=user.id, plan=payload.plan, status=SubscriptionStatus.pending,
        provider=gateway, amount_rub=PLAN_PRICES_RUB[payload.plan],
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)

    try:
        if payload.provider == "yookassa":
            payment = billing.create_payment(payload.plan, subscription.id)
            confirmation_url = payment.get("confirmation", {}).get("confirmation_url")
            external_id = payment["id"]
        elif payload.provider == "sbp":
            payment = billing.create_payment(payload.plan, subscription.id, payment_method_type="sbp")
            confirmation_url = payment.get("confirmation", {}).get("confirmation_url")
            external_id = payment["id"]
        else:
            payment = billing.create_crypto_payment(payload.plan, subscription.id)
            confirmation_url = payment.get("url")
            external_id = payment["uuid"]
    except billing.BillingError as exc:
        db.delete(subscription)
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc))

    subscription.external_payment_id = external_id
    db.commit()

    return SubscriptionOut(
        id=subscription.id, plan=subscription.plan, status=subscription.status,
        amount_rub=subscription.amount_rub, confirmation_url=confirmation_url,
    )


@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    payment_id = body.get("object", {}).get("id")
    if not payment_id:
        raise HTTPException(status_code=400, detail="В теле вебхука нет object.id")

    # Never trust the webhook body for the actual status — ask ЮKassa directly.
    try:
        payment = billing.fetch_payment(payment_id)
    except billing.BillingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    subscription = db.query(Subscription).filter_by(
        external_payment_id=payment_id, provider="yookassa"
    ).first()
    if not subscription:
        return {"ignored": "unknown payment_id"}

    if payment.get("status") == "succeeded" and subscription.status != SubscriptionStatus.active:
        _activate_subscription(db, subscription)
    elif payment.get("status") == "canceled":
        subscription.status = SubscriptionStatus.canceled
        db.commit()

    return {"status": "ok"}


@app.post("/webhook/cryptomus")
async def cryptomus_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()

    if not billing.verify_cryptomus_signature(body):
        raise HTTPException(status_code=401, detail="Неверная подпись вебхука Cryptomus")

    payment_uuid = body.get("uuid")
    status = body.get("status")  # "paid", "paid_over", "wrong_amount", "cancel", "fail" — see Cryptomus docs
    if not payment_uuid:
        raise HTTPException(status_code=400, detail="В теле вебхука нет uuid")

    subscription = db.query(Subscription).filter_by(
        external_payment_id=payment_uuid, provider="cryptomus"
    ).first()
    if not subscription:
        return {"ignored": "unknown payment_id"}

    if status in ("paid", "paid_over") and subscription.status != SubscriptionStatus.active:
        _activate_subscription(db, subscription)
    elif status in ("cancel", "fail"):
        subscription.status = SubscriptionStatus.canceled
        db.commit()

    return {"status": "ok"}


def _activate_subscription(db: Session, subscription: Subscription) -> None:
    subscription.status = SubscriptionStatus.active
    subscription.activated_at = datetime.now(timezone.utc)
    subscription.expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    subscription.user.plan = subscription.plan
    db.commit()


# ---------- Admin (existing, unchanged behaviour) ----------

@app.post("/projects", response_model=ProjectOut, dependencies=[Depends(require_admin)])
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    if db.query(Project).filter_by(slug=payload.slug).first():
        raise HTTPException(status_code=409, detail="Проект с таким slug уже существует")

    repo_url = payload.repo_url or f"https://github.com/{payload.repo_full_name}.git"

    project = Project(
        slug=payload.slug,
        repo_url=repo_url,
        branch=payload.branch,
        kind=payload.kind,
        env_json=json.dumps(payload.env),
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return _to_project_out(project)


@app.get("/projects", response_model=list[ProjectOut], dependencies=[Depends(require_admin)])
def list_projects(db: Session = Depends(get_db)):
    return [_to_project_out(p) for p in db.query(Project).all()]


@app.delete("/projects/{slug}", dependencies=[Depends(require_admin)])
def delete_project(slug: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter_by(slug=slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден")
    from app import deployer
    deployer.stop_and_remove(project.slug)
    db.delete(project)
    db.commit()
    return {"deleted": slug}


@app.get("/projects/{slug}/deployments", response_model=list[DeploymentOut], dependencies=[Depends(require_admin)])
def list_deployments(slug: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter_by(slug=slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return [_to_deployment_out(d) for d in project.deployments]


@app.post("/webhook/github/{slug}")
async def github_webhook(
    slug: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    project = db.query(Project).filter_by(slug=slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден")

    body = await request.body()
    if not verify_signature(body, project.webhook_secret, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Неверная подпись вебхука")

    if x_github_event == "ping":
        return {"pong": True}

    if x_github_event != "push":
        return {"ignored": x_github_event}

    payload = json.loads(body or b"{}")
    branch, commit_sha = extract_push_info(payload)
    if branch != project.branch:
        return {"ignored": f"push to {branch}, watching {project.branch}"}

    deployment = Deployment(project_id=project.id, commit_sha=commit_sha, status=DeployStatus.pending)
    db.add(deployment)
    db.commit()
    db.refresh(deployment)

    # Respond to GitHub immediately; the actual build/deploy runs after the response.
    background_tasks.add_task(run_deploy, db, project, deployment)

    return {"deployment_id": deployment.id, "status": deployment.status}


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$")


def _validate_domain(raw: str) -> str:
    domain = raw.strip().lower().rstrip(".")
    if domain == DOMAIN_SUFFIX or domain.endswith(f".{DOMAIN_SUFFIX}"):
        raise HTTPException(
            status_code=400,
            detail=f"Нельзя привязать поддомен {DOMAIN_SUFFIX} этим способом — он уже твой по умолчанию",
        )
    if not _DOMAIN_RE.match(domain):
        raise HTTPException(status_code=400, detail="Похоже, это не настоящий домен — например, example.com")
    return domain


def _to_project_out(p: Project) -> ProjectOut:
    return ProjectOut(
        id=p.id, slug=p.slug, repo_url=p.repo_url, branch=p.branch, kind=p.kind,
        webhook_secret=p.webhook_secret, webhook_auto_configured=p.webhook_auto_configured,
        custom_domain=p.custom_domain,
        url=f"https://{p.slug}.{DOMAIN_SUFFIX}",
        custom_domain_url=f"https://{p.custom_domain}" if p.custom_domain else None,
    )


def _to_deployment_out(d: Deployment) -> DeploymentOut:
    return DeploymentOut(
        id=d.id, project_id=d.project_id, commit_sha=d.commit_sha, status=d.status,
        log=d.log, port=d.port,
        created_at=d.created_at.isoformat(), finished_at=d.finished_at.isoformat() if d.finished_at else None,
    )
