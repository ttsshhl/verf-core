import subprocess
from pathlib import Path

import pytest

from app import builder
from app.builder import BuildError


@pytest.fixture
def fake_upstream(tmp_path: Path) -> Path:
    """A real local git repo we clone from, standing in for a GitHub URL."""
    repo = tmp_path / "upstream.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "requirements.txt").write_text("fastapi\n")
    (repo / "main.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture(autouse=True)
def isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")


def test_clone_creates_project_dir_and_returns_sha(fake_upstream):
    sha = builder.clone_or_pull("proj1", str(fake_upstream), "main")
    assert len(sha) == 40
    assert (builder.project_dir("proj1") / "main.py").exists()


def test_pull_updates_existing_checkout(fake_upstream):
    sha1 = builder.clone_or_pull("proj2", str(fake_upstream), "main")

    (fake_upstream / "main.py").write_text("print('updated')\n")
    subprocess.run(["git", "add", "."], cwd=fake_upstream, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "update"], cwd=fake_upstream, check=True)

    sha2 = builder.clone_or_pull("proj2", str(fake_upstream), "main")
    assert sha1 != sha2
    content = (builder.project_dir("proj2") / "main.py").read_text()
    assert "updated" in content


def test_clone_bad_url_raises_builderror(tmp_path):
    with pytest.raises(BuildError):
        builder.clone_or_pull("proj3", str(tmp_path / "does-not-exist"), "main")


def test_detect_profile_python(fake_upstream):
    builder.clone_or_pull("proj4", str(fake_upstream), "main")
    profile = builder.detect_profile("proj4")
    assert profile.kind == "python"
    assert profile.internal_port == 8000


def test_detect_profile_node(tmp_path, monkeypatch):
    slug = "proj5"
    d = builder.project_dir(slug)
    d.mkdir(parents=True)
    (d / "package.json").write_text("{}")
    profile = builder.detect_profile(slug)
    assert profile.kind == "node"
    assert profile.internal_port == 3000


def test_detect_profile_static(tmp_path):
    slug = "proj6"
    d = builder.project_dir(slug)
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html></html>")
    profile = builder.detect_profile(slug)
    assert profile.kind == "static"


def test_detect_profile_existing_dockerfile_wins(tmp_path):
    slug = "proj7"
    d = builder.project_dir(slug)
    d.mkdir(parents=True)
    (d / "package.json").write_text("{}")
    (d / "Dockerfile").write_text("FROM scratch\n")
    profile = builder.detect_profile(slug)
    assert profile.kind == "dockerfile"


def test_detect_profile_unknown_raises(tmp_path):
    slug = "proj8"
    builder.project_dir(slug).mkdir(parents=True)
    with pytest.raises(BuildError):
        builder.detect_profile(slug)


def test_ensure_dockerfile_writes_generated_file(tmp_path):
    slug = "proj9"
    d = builder.project_dir(slug)
    d.mkdir(parents=True)
    (d / "requirements.txt").write_text("fastapi\n")
    (d / "main.py").write_text("print('hi')\n")
    profile = builder.detect_profile(slug)
    builder.ensure_dockerfile(slug, profile)
    assert (d / "Dockerfile").exists()
    assert "python" in (d / "Dockerfile").read_text().lower()
