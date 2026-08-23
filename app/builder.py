"""Git operations + project-type detection for the deploy pipeline.

Everything in this module is pure filesystem/subprocess work — no Docker
required — so it's fully testable without a Docker daemon.
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import WORKSPACE_DIR


class BuildError(Exception):
    pass


def project_dir(slug: str) -> Path:
    return WORKSPACE_DIR / slug


def clone_or_pull(slug: str, repo_url: str, branch: str = "main") -> str:
    """Clone the repo on first deploy, or fetch+reset on subsequent ones.

    Returns the resolved commit SHA that was checked out.
    """
    target = project_dir(slug)

    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--branch", branch, "--depth", "1", repo_url, str(target)])
    else:
        _run(["git", "fetch", "origin", branch], cwd=target)
        _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=target)

    sha = _run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip()
    return sha


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise BuildError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


@dataclass
class ProjectProfile:
    kind: str          # "static" | "node" | "python" | "dockerfile"
    dockerfile: str     # contents to write if repo has no Dockerfile
    internal_port: int  # port the app is expected to listen on inside the container


def detect_profile(slug: str) -> ProjectProfile:
    """Inspect the cloned repo and decide how to build/run it."""
    root = project_dir(slug)

    if (root / "Dockerfile").exists():
        return ProjectProfile(kind="dockerfile", dockerfile="", internal_port=8080)

    if (root / "package.json").exists():
        return ProjectProfile(
            kind="node",
            dockerfile=_NODE_DOCKERFILE,
            internal_port=3000,
        )

    if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists():
        return ProjectProfile(
            kind="python",
            dockerfile=_PYTHON_DOCKERFILE,
            internal_port=8000,
        )

    if (root / "index.html").exists():
        return ProjectProfile(
            kind="static",
            dockerfile=_STATIC_DOCKERFILE,
            internal_port=80,
        )

    raise BuildError(
        "Не удалось определить тип проекта: нет Dockerfile, package.json, "
        "requirements.txt/pyproject.toml или index.html в корне репозитория."
    )


def ensure_dockerfile(slug: str, profile: ProjectProfile) -> None:
    if profile.kind == "dockerfile":
        return  # repo already brings its own
    (project_dir(slug) / "Dockerfile").write_text(profile.dockerfile)


_NODE_DOCKERFILE = """FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev || npm install --omit=dev
COPY . .
EXPOSE 3000
CMD ["npm", "start"]
"""

_PYTHON_DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt* pyproject.toml* ./
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY . .
EXPOSE 8000
CMD ["python", "main.py"]
"""

_STATIC_DOCKERFILE = """FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
"""
