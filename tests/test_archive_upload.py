import zipfile

import pytest

from app import builder
from app.builder import BuildError


@pytest.fixture(autouse=True)
def isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "WORKSPACE_DIR", tmp_path / "workspace")


def _make_zip(tmp_path, name: str, files: dict[str, str], top_level_folder: str | None = None) -> "Path":
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for rel_path, content in files.items():
            arcname = f"{top_level_folder}/{rel_path}" if top_level_folder else rel_path
            zf.writestr(arcname, content)
    return zip_path


def test_replace_from_archive_extracts_files(tmp_path):
    zip_path = _make_zip(tmp_path, "proj.zip", {"index.html": "<h1>hi</h1>", "sub/file.txt": "x"})
    sha = builder.replace_from_archive("myproj", zip_path)
    root = builder.project_dir("myproj")
    assert (root / "index.html").read_text() == "<h1>hi</h1>"
    assert (root / "sub" / "file.txt").read_text() == "x"
    assert sha.startswith("upload-")


def test_replace_from_archive_flattens_single_top_level_folder(tmp_path):
    """Common case: someone right-clicks their project folder and zips it,
    so the archive contains one wrapper folder instead of files at the root."""
    zip_path = _make_zip(tmp_path, "proj.zip", {"index.html": "<h1>hi</h1>"}, top_level_folder="my-project")
    builder.replace_from_archive("flatten-test", zip_path)
    root = builder.project_dir("flatten-test")
    assert (root / "index.html").exists()
    assert not (root / "my-project").exists()


def test_replace_from_archive_wipes_previous_content(tmp_path):
    zip1 = _make_zip(tmp_path, "v1.zip", {"old.txt": "old"})
    builder.replace_from_archive("versioned", zip1)
    assert (builder.project_dir("versioned") / "old.txt").exists()

    zip2 = _make_zip(tmp_path, "v2.zip", {"new.txt": "new"})
    builder.replace_from_archive("versioned", zip2)
    root = builder.project_dir("versioned")
    assert (root / "new.txt").exists()
    assert not (root / "old.txt").exists()  # previous deploy's files are gone, not merged


def test_replace_from_archive_rejects_non_zip(tmp_path):
    fake = tmp_path / "not-a-zip.zip"
    fake.write_text("just some text, not a real zip file")
    with pytest.raises(BuildError):
        builder.replace_from_archive("badzip", fake)


def test_replace_from_archive_blocks_zip_slip(tmp_path):
    """A malicious archive with a path like '../../etc/evil' must not be
    allowed to write outside the project's own directory."""
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../../../etc/evil.txt", "pwned")

    with pytest.raises(BuildError):
        builder.replace_from_archive("victim", zip_path)


def test_replace_from_archive_returns_stable_sha_for_identical_content(tmp_path):
    zip1 = _make_zip(tmp_path, "a.zip", {"x.txt": "same content"})
    zip2 = _make_zip(tmp_path, "b.zip", {"x.txt": "same content"})
    sha1 = builder.replace_from_archive("proj-a", zip1)
    sha2 = builder.replace_from_archive("proj-b", zip2)
    assert sha1 == sha2  # same bytes -> same content hash, regardless of filename


# ---------- macOS/Windows zip metadata junk ----------

def test_replace_from_archive_strips_macos_metadata_folder(tmp_path):
    """The real-world bug this fixes: a zip made with macOS Finder's
    "Compress" feature includes a __MACOSX folder alongside the real
    project folder — without stripping it, the "flatten single wrapper
    folder" logic never triggers (two top-level entries, not one), leaving
    index.html nested one level too deep for profile detection to find."""
    zip_path = tmp_path / "site.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("mysite/index.html", "<h1>hi</h1>")
        zf.writestr("__MACOSX/mysite/._index.html", "junk")

    builder.replace_from_archive("macos-site", zip_path)
    root = builder.project_dir("macos-site")
    assert (root / "index.html").exists()
    assert not (root / "__MACOSX").exists()
    assert not (root / "mysite").exists()  # correctly flattened, not left nested


def test_replace_from_archive_strips_ds_store_at_root():
    zip_path_files = {"index.html": "<h1>hi</h1>", ".DS_Store": "junk"}
    zip_path = builder.project_dir("temp-holder").parent / "ds-store-test.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in zip_path_files.items():
            zf.writestr(name, content)

    builder.replace_from_archive("ds-store-proj", zip_path)
    root = builder.project_dir("ds-store-proj")
    assert (root / "index.html").exists()
    assert not (root / ".DS_Store").exists()


def test_replace_from_archive_still_flattens_when_only_junk_and_one_real_folder(tmp_path):
    """Confirms the fix actually restores flattening in the exact scenario
    that was broken: __MACOSX + one real wrapper folder = should still
    flatten the real folder despite __MACOSX also being present at root."""
    zip_path = tmp_path / "site2.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("real-project/index.html", "<h1>hi</h1>")
        zf.writestr("real-project/style.css", "body{}")
        zf.writestr("__MACOSX/real-project/._index.html", "junk")
        zf.writestr("__MACOSX/real-project/._style.css", "junk")

    builder.replace_from_archive("flatten-with-junk", zip_path)
    root = builder.project_dir("flatten-with-junk")
    assert (root / "index.html").exists()
    assert (root / "style.css").exists()
    assert not (root / "__MACOSX").exists()
    assert not (root / "real-project").exists()
