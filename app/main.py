import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, BackgroundTasks, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app import auth, billing
from app.config import ADMIN_API_KEY, DOMAIN_SUFFIX, PLAN_PROJECT_LIMITS
from app.db import get_db, init_db
from app.models import Project, Deployment, DeployStatus, User, Subscription, SubscriptionStatus
from app.pipeline import run_deploy
from app.schemas import (
    ProjectCreate, ProjectOut, DeploymentOut,
    UserCreate, UserLogin, UserOut, TokenOut, SubscribeRequest, SubscriptionOut,
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

    project = Project(
        owner_id=user.id, slug=payload.slug, repo_url=payload.repo_url, branch=payload.branch,
        kind=payload.kind, env_json=json.dumps(payload.env),
    )
    db.add(project)
    db.commit()
    db.refresh(project)
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
    if payload.provider not in ("yookassa", "cryptomus"):
        raise HTTPException(status_code=400, detail="provider должен быть 'yookassa' или 'cryptomus'")

    subscription = Subscription(
        user_id=user.id, plan=payload.plan, status=SubscriptionStatus.pending,
        provider=payload.provider, amount_rub=PLAN_PRICES_RUB[payload.plan],
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)

    try:
        if payload.provider == "yookassa":
            payment = billing.create_payment(payload.plan, subscription.id)
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

    project = Project(
        slug=payload.slug,
        repo_url=payload.repo_url,
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


def _to_project_out(p: Project) -> ProjectOut:
    return ProjectOut(
        id=p.id, slug=p.slug, repo_url=p.repo_url, branch=p.branch, kind=p.kind,
        webhook_secret=p.webhook_secret, url=f"https://{p.slug}.{DOMAIN_SUFFIX}",
    )


def _to_deployment_out(d: Deployment) -> DeploymentOut:
    return DeploymentOut(
        id=d.id, project_id=d.project_id, commit_sha=d.commit_sha, status=d.status,
        log=d.log, port=d.port,
        created_at=d.created_at.isoformat(), finished_at=d.finished_at.isoformat() if d.finished_at else None,
    )
