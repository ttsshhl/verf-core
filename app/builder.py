"""Git operations + project-type detection for the deploy pipeline.

Everything in this module is pure filesystem/subprocess work — no Docker
required — so it's fully testable without a Docker daemon.
"""
import hashlib
import shutil
import subprocess
import zipfile
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


def replace_from_archive(slug: str, archive_path: Path) -> str:
    """Replaces the project's working directory with the contents of a ZIP
    archive — the CLI / cabinet-upload equivalent of clone_or_pull.

    Returns a short pseudo-version identifier (sha256 of the archive bytes,
    truncated) so deployments from uploads get a stable, comparable "commit_sha"
    the same way git-based deploys do, even though there's no real commit.
    """
    if not zipfile.is_zipfile(archive_path):
        raise BuildError("Загруженный файл не является ZIP-архивом")

    target = project_dir(slug)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive_path) as zf:
            _safe_extract(zf, target)
    except zipfile.BadZipFile as exc:
        raise BuildError(f"Не удалось распаковать архив: {exc}")

    _strip_archive_junk(target)

    # If the zip contained a single top-level folder (the common case when
    # someone zips a project folder in Finder/Explorer), flatten it so
    # project files end up directly in project_dir instead of nested one
    # level deeper than every other deploy path expects. Junk entries
    # (above) are stripped *before* this check specifically because macOS's
    # built-in "Compress" adds a __MACOSX folder alongside the real one —
    # without stripping it first, there'd be two top-level entries instead
    # of one, and this flattening would never trigger.
    entries = list(target.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(target / item.name))
        inner.rmdir()

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    return f"upload-{digest[:12]}"


_ARCHIVE_JUNK_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}


def _strip_archive_junk(target: Path) -> None:
    """Removes metadata cruft that common zip tools (macOS Finder, Windows
    Explorer) add alongside real project files — never part of the actual
    project, and left in place they break both flattening (see above) and,
    for __MACOSX specifically, could confuse profile detection if it ever
    contains a stray file matching one of our detection filenames."""
    for entry in list(target.iterdir()):
        if entry.name in _ARCHIVE_JUNK_NAMES:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()


def _safe_extract(zf: zipfile.ZipFile, target: Path) -> None:
    """Extracts a zip while refusing entries that would escape `target`
    (zip-slip protection) — the archive comes from a user upload, not a
    trusted source, so path traversal must be blocked explicitly.
    """
    target_resolved = target.resolve()
    for member in zf.namelist():
        member_path = (target / member).resolve()
        if not str(member_path).startswith(str(target_resolved)):
            raise BuildError(f"Небезопасный путь в архиве: {member}")
    zf.extractall(target)


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
        entrypoint = _detect_python_entrypoint(root)
        return ProjectProfile(
            kind="python",
            dockerfile=_PYTHON_DOCKERFILE_TEMPLATE.format(entrypoint=entrypoint),
            internal_port=8000,
        )

    if (root / "index.html").exists():
        return ProjectProfile(
            kind="static",
            dockerfile=_STATIC_DOCKERFILE_TEMPLATE.format(index_fixup=""),
            internal_port=80,
        )

    # No index.html — but if there's exactly one other HTML file, that's
    # almost certainly meant to be the site (a single-page site someone
    # named after its content, e.g. "pure-cleaning.html", rather than the
    # platform convention). Copy it to index.html at build time so nginx's
    # default document resolution just works, instead of failing outright.
    html_files = sorted(
        p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() in (".html", ".htm")
    )
    if len(html_files) == 1:
        return ProjectProfile(
            kind="static",
            dockerfile=_STATIC_DOCKERFILE_TEMPLATE.format(
                index_fixup=f"RUN cp /usr/share/nginx/html/{html_files[0]} /usr/share/nginx/html/index.html\n"
            ),
            internal_port=80,
        )
    if len(html_files) > 1:
        raise BuildError(
            "Несколько HTML-файлов в корне, но ни один не называется index.html — "
            "переименуй главную страницу в index.html, чтобы платформа знала, что открывать по умолчанию."
        )

    raise BuildError(
        "Не удалось определить тип проекта: нет Dockerfile, package.json, "
        "requirements.txt/pyproject.toml или HTML-файла в корне репозитория."
    )


# Порядок важен — main.py остаётся самым приоритетным (наша собственная
# конвенция для CLI/ZIP-проектов без своего Dockerfile), но если пользователь
# принёс типовой шаблон бота откуда-то ещё, который называет входной файл
# иначе — подхватываем и его, вместо того чтобы молча падать.
_PYTHON_ENTRYPOINT_CANDIDATES = ["main.py", "bot.py", "app.py", "run.py", "__main__.py"]


def _detect_python_entrypoint(root: Path) -> str:
    for name in _PYTHON_ENTRYPOINT_CANDIDATES:
        if (root / name).exists():
            return name
    raise BuildError(
        "Не найден входной файл Python-проекта — ожидается один из: "
        + ", ".join(_PYTHON_ENTRYPOINT_CANDIDATES)
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

_PYTHON_DOCKERFILE_TEMPLATE = """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt* pyproject.toml* ./
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY . .
EXPOSE 8000
CMD ["python", "{entrypoint}"]
"""

_STATIC_DOCKERFILE_TEMPLATE = """FROM nginx:alpine
COPY . /usr/share/nginx/html
{index_fixup}EXPOSE 80
"""
