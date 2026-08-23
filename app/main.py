import json

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, BackgroundTasks, Header
from sqlalchemy.orm import Session

from app.config import ADMIN_API_KEY, DOMAIN_SUFFIX
from app.db import get_db, init_db
from app.models import Project, Deployment, DeployStatus
from app.pipeline import run_deploy
from app.schemas import ProjectCreate, ProjectOut, DeploymentOut
from app.webhook import verify_signature, extract_push_info


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="VERF core", version="0.1.0", lifespan=lifespan)


def require_admin(x_api_key: str | None = Header(default=None)):
    if not ADMIN_API_KEY:
        return  # local dev — no admin key configured
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий X-API-Key")


@app.get("/health")
def health():
    return {"status": "ok"}


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
