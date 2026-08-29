from pathlib import Path

import pytest

from app import builder
from app.builder import BuildError


@pytest.fixture(autouse=True)
def isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")


def _make_project(slug: str, files: dict[str, str]) -> Path:
    root = builder.project_dir(slug)
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content)
    return root


# ---------- flexible Python entrypoint detection ----------

def test_detects_main_py_as_entrypoint():
    _make_project("proj-main", {"requirements.txt": "fastapi\n", "main.py": "print('hi')"})
    profile = builder.detect_profile("proj-main")
    assert 'CMD ["python", "main.py"]' in profile.dockerfile


def test_detects_bot_py_as_entrypoint():
    """The exact real-world case this feature fixes: a downloaded bot
    template using bot.py instead of main.py, with no Dockerfile of its own."""
    _make_project("proj-bot", {"requirements.txt": "aiogram\n", "bot.py": "print('hi')"})
    profile = builder.detect_profile("proj-bot")
    assert 'CMD ["python", "bot.py"]' in profile.dockerfile


def test_detects_app_py_as_entrypoint():
    _make_project("proj-app", {"requirements.txt": "flask\n", "app.py": "print('hi')"})
    profile = builder.detect_profile("proj-app")
    assert 'CMD ["python", "app.py"]' in profile.dockerfile


def test_main_py_takes_priority_when_multiple_candidates_present():
    """If a project happens to have both (e.g. a helper script alongside
    the real entrypoint), our own convention (main.py) wins — predictable
    beats clever."""
    _make_project("proj-both", {
        "requirements.txt": "fastapi\n", "main.py": "print('main')", "app.py": "print('app')",
    })
    profile = builder.detect_profile("proj-both")
    assert 'CMD ["python", "main.py"]' in profile.dockerfile


def test_raises_clear_error_when_no_entrypoint_found():
    _make_project("proj-none", {"requirements.txt": "fastapi\n", "readme.txt": "hi"})
    with pytest.raises(BuildError) as exc_info:
        builder.detect_profile("proj-none")
    assert "main.py" in str(exc_info.value)
    assert "bot.py" in str(exc_info.value)


def test_projects_with_their_own_dockerfile_are_left_untouched():
    """A user-supplied Dockerfile is used as-is regardless of what Python
    entrypoint it references — we never second-guess it."""
    _make_project("proj-owndocker", {
        "requirements.txt": "fastapi\n",
        "Dockerfile": "FROM python:3.12-slim\nCMD [\"python\", \"whatever_name.py\"]\n",
    })
    profile = builder.detect_profile("proj-owndocker")
    assert profile.kind == "dockerfile"
