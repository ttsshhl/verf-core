import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import builder
from app.builder import BuildError
from app.models import Deployment, DeployStatus, Project


def run_deploy(db: Session, project: Project, deployment: Deployment) -> None:
    """Runs the full pipeline synchronously, updating `deployment` as it goes.

    Called from a FastAPI BackgroundTask so the webhook responds to GitHub
    immediately (GitHub expects a fast response and retries/queues otherwise).
    """
    def log(line: str) -> None:
        deployment.log = (deployment.log or "") + line + "\n"
        db.commit()

    def set_status(status: DeployStatus) -> None:
        deployment.status = status
        db.commit()

    try:
        set_status(DeployStatus.cloning)
        log(f"→ Клонирую {project.repo_url} ({project.branch})")
        sha = builder.clone_or_pull(project.slug, project.repo_url, project.branch)
        deployment.commit_sha = sha
        log(f"→ Коммит {sha[:8]}")

        profile = builder.detect_profile(project.slug)
        log(f"→ Тип проекта: {profile.kind}")
        builder.ensure_dockerfile(project.slug, profile)

        set_status(DeployStatus.building)
        log("→ Собираю образ...")
        from app import deployer  # imported here so tests can run pipeline logic up to this point without docker installed
        image_tag = deployer.build_image(project.slug, deployment.id)
        deployment.image_tag = image_tag
        log(f"→ Образ собран: {image_tag}")

        set_status(DeployStatus.starting)
        env = json.loads(project.env_json or "{}")

        from app.config import PLAN_MEM_LIMITS, PLAN_CPU_QUOTAS, DEFAULT_MEM_LIMIT, DEFAULT_CPU_QUOTA
        owner_plan = project.owner.plan if project.owner else "free"
        mem_limit = PLAN_MEM_LIMITS.get(owner_plan, DEFAULT_MEM_LIMIT)
        cpu_quota = PLAN_CPU_QUOTAS.get(owner_plan, DEFAULT_CPU_QUOTA)

        container_id = deployer.run_container(
            project.slug, image_tag, profile.internal_port, env,
            mem_limit=mem_limit, cpu_quota=cpu_quota,
        )
        deployment.container_id = container_id
        deployment.port = profile.internal_port
        log(f"→ Контейнер запущен: {container_id[:12]} (тариф «{owner_plan}»: {mem_limit} RAM)")
        log(f"🟢 Живой: https://{project.slug}.{{DOMAIN}}")

        set_status(DeployStatus.running)

    except BuildError as exc:
        log(f"✗ Ошибка сборки: {exc}")
        set_status(DeployStatus.failed)
    except Exception as exc:  # deployer.DeployError and anything unexpected
        log(f"✗ Ошибка деплоя: {exc}")
        set_status(DeployStatus.failed)
    finally:
        deployment.finished_at = datetime.now(timezone.utc)
        db.commit()
