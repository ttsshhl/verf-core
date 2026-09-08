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


# ---------- flexible static-site entrypoint detection ----------

def test_detects_index_html_as_before(tmp_path, monkeypatch):
    from app import builder
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    root = builder.project_dir("static-index")
    root.mkdir(parents=True)
    (root / "index.html").write_text("<h1>hi</h1>")
    profile = builder.detect_profile("static-index")
    assert profile.kind == "static"
    assert "RUN cp" not in profile.dockerfile  # no fixup needed — already named correctly


def test_detects_single_nonstandard_html_file_as_static_entrypoint(tmp_path, monkeypatch):
    """The real-world case this fixes: a single-page site named after its
    content ("pure-cleaning.html") instead of the platform convention."""
    from app import builder
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    root = builder.project_dir("static-custom-name")
    root.mkdir(parents=True)
    (root / "pure-cleaning.html").write_text("<h1>Clean!</h1>")
    profile = builder.detect_profile("static-custom-name")
    assert profile.kind == "static"
    assert "cp /usr/share/nginx/html/pure-cleaning.html /usr/share/nginx/html/index.html" in profile.dockerfile


def test_multiple_html_files_without_index_raises_clear_error(tmp_path, monkeypatch):
    """Genuinely ambiguous — which page is "home"? Don't guess."""
    from app import builder
    from app.builder import BuildError
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    root = builder.project_dir("static-ambiguous")
    root.mkdir(parents=True)
    (root / "about.html").write_text("<h1>About</h1>")
    (root / "contact.html").write_text("<h1>Contact</h1>")
    with pytest.raises(BuildError) as exc_info:
        builder.detect_profile("static-ambiguous")
    assert "index.html" in str(exc_info.value)


def test_no_html_at_all_still_raises_original_error(tmp_path, monkeypatch):
    from app import builder
    from app.builder import BuildError
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    root = builder.project_dir("static-none")
    root.mkdir(parents=True)
    (root / "readme.txt").write_text("hi")
    with pytest.raises(BuildError):
        builder.detect_profile("static-none")


# ---------- port detection from user-supplied Dockerfile (EXPOSE) ----------

def test_detects_exposed_port_from_own_dockerfile(tmp_path, monkeypatch):
    """Regression test for a real incident: a project with its own
    Dockerfile whose app listened on 3000, while we blindly routed
    Traefik to 8080 — Bad Gateway until manually diagnosed."""
    from app import builder
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    root = builder.project_dir("own-dockerfile-3000")
    root.mkdir(parents=True)
    (root / "Dockerfile").write_text("FROM node:20-alpine\nEXPOSE 3000\nCMD [\"node\", \"server.mjs\"]\n")
    profile = builder.detect_profile("own-dockerfile-3000")
    assert profile.kind == "dockerfile"
    assert profile.internal_port == 3000


def test_falls_back_to_8080_when_no_expose_present(tmp_path, monkeypatch):
    from app import builder
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    root = builder.project_dir("own-dockerfile-noexpose")
    root.mkdir(parents=True)
    (root / "Dockerfile").write_text("FROM alpine\nCMD [\"true\"]\n")
    profile = builder.detect_profile("own-dockerfile-noexpose")
    assert profile.internal_port == 8080


def test_last_expose_wins_when_multiple_present(tmp_path, monkeypatch):
    """Matches Docker's own behaviour — the last EXPOSE is the one that
    actually takes effect if a Dockerfile declares more than one."""
    from app import builder
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    root = builder.project_dir("own-dockerfile-multi")
    root.mkdir(parents=True)
    (root / "Dockerfile").write_text("FROM alpine\nEXPOSE 8080\nEXPOSE 5000\nCMD [\"true\"]\n")
    profile = builder.detect_profile("own-dockerfile-multi")
    assert profile.internal_port == 5000


def test_expose_detection_is_case_insensitive(tmp_path, monkeypatch):
    from app import builder
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")
    root = builder.project_dir("own-dockerfile-lowercase")
    root.mkdir(parents=True)
    (root / "Dockerfile").write_text("from alpine\nexpose 4000\ncmd [\"true\"]\n")
    profile = builder.detect_profile("own-dockerfile-lowercase")
    assert profile.internal_port == 4000


def test_expose_detection_ignores_leading_whitespace():
    from app.builder import _detect_exposed_port
    assert _detect_exposed_port("FROM alpine\n    EXPOSE 9000\n") == 9000
