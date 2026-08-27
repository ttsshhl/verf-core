#!/usr/bin/env python3
"""VERF CLI — деплой проектов на VERF без GitHub.

Никаких зависимостей, кроме стандартной библиотеки Python — просто скачай
этот файл и запусти:

    python3 verf.py login
    python3 verf.py create my-bot --kind bot
    cd my-bot/
    python3 verf.py deploy my-bot

Полный список команд: python3 verf.py --help
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Optional

API_URL = os.environ.get("VERF_API_URL", "https://api.verfdeploy.ru")
CONFIG_DIR = Path.home() / ".verf"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Папки/файлы, которые почти никогда не должны попадать в деплой-архив.
DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "venv", ".venv",
    "env", ".idea", ".vscode", "dist", "build", ".verf",
}
DEFAULT_IGNORE_SUFFIXES = {".pyc"}
DEFAULT_IGNORE_NAMES = {".DS_Store"}


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())


def _save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass  # best-effort — some platforms (older Windows setups) don't support POSIX perms


def _token() -> str:
    token = _load_config().get("token")
    if not token:
        print("Не авторизован. Сначала выполни: python3 verf.py login")
        sys.exit(1)
    return token


def _api_request(method: str, path: str, token: Optional[str] = None, json_body: Optional[dict] = None) -> dict:
    url = f"{API_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(json_body).encode() if json_body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        _print_api_error(e)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Не удалось подключиться к {API_URL}: {e.reason}")
        sys.exit(1)


def _print_api_error(e: urllib.error.HTTPError) -> None:
    body = e.read().decode()
    try:
        detail = json.loads(body).get("detail", body)
    except json.JSONDecodeError:
        detail = body
    print(f"Ошибка API ({e.code}): {detail}")


def _upload_archive(path: str, token: str, archive_path: Path) -> dict:
    boundary = uuid.uuid4().hex
    filename = archive_path.name

    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="archive"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/zip\r\n\r\n"
    body += archive_path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{API_URL}{path}",
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        _print_api_error(e)
        sys.exit(1)


def _should_ignore(rel_path: Path) -> bool:
    if rel_path.name in DEFAULT_IGNORE_NAMES:
        return True
    if rel_path.suffix in DEFAULT_IGNORE_SUFFIXES:
        return True
    return any(part in DEFAULT_IGNORE_DIRS for part in rel_path.parts)


def _zip_current_dir(target_zip: Path) -> int:
    cwd = Path.cwd()
    count = 0
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in cwd.rglob("*"):
            if path.is_dir() or path == target_zip:
                continue
            rel = path.relative_to(cwd)
            if _should_ignore(rel):
                continue
            zf.write(path, rel)
            count += 1
    return count


# ---------- commands ----------

def cmd_login(args):
    email = input("Почта: ").strip()
    password = getpass.getpass("Пароль: ")
    result = _api_request("POST", "/auth/login", json_body={"email": email, "password": password})
    _save_config({"token": result["access_token"], "email": email})
    print("Готово — вход выполнен.")


def cmd_logout(args):
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    print("Вышел.")


def cmd_projects(args):
    token = _token()
    projects = _api_request("GET", "/me/projects", token=token)
    if not projects:
        print("Проектов пока нет. Создай: python3 verf.py create <slug>")
        return
    for p in projects:
        print(f"{p['slug']:<24} {p['kind']:<10} {p['url']}")


def cmd_create(args):
    token = _token()
    payload = {"slug": args.slug, "kind": args.kind, "branch": "main"}
    project = _api_request("POST", "/me/projects", token=token, json_body=payload)
    print(f"Создан проект «{project['slug']}» → {project['url']}")
    print(f"Задеплой его: python3 verf.py deploy {project['slug']}")


def cmd_deploy(args):
    token = _token()
    slug = args.slug

    print(f"→ Собираю архив из {Path.cwd()}")
    tmp_zip = Path.cwd() / f".verf-deploy-{uuid.uuid4().hex[:8]}.zip"
    try:
        file_count = _zip_current_dir(tmp_zip)
        if file_count == 0:
            print("Папка пуста (или всё попало в исключения вроде node_modules/.git) — нечего деплоить.")
            sys.exit(1)
        size_mb = tmp_zip.stat().st_size / (1024 * 1024)
        print(f"→ Архив собран: {file_count} файлов, {size_mb:.1f} МБ")

        print("→ Загружаю на VERF...")
        deployment = _upload_archive(f"/me/projects/{slug}/deploy", token, tmp_zip)
    finally:
        tmp_zip.unlink(missing_ok=True)

    deployment_id = deployment["id"]
    print(f"→ Деплой запущен ({deployment_id[:8]})\n")

    seen_log_len = 0
    while True:
        time.sleep(2)
        deployment = _api_request("GET", f"/me/deployments/{deployment_id}", token=token)
        log = deployment.get("log") or ""
        if len(log) > seen_log_len:
            print(log[seen_log_len:], end="")
            seen_log_len = len(log)
        if deployment["status"] in ("running", "failed"):
            break

    if deployment["status"] == "running":
        print("\n✅ Деплой завершён")
        sys.exit(0)
    else:
        print("\n❌ Деплой не удался — см. лог выше")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(prog="verf", description="VERF CLI — деплой без GitHub")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Войти в аккаунт VERF").set_defaults(func=cmd_login)
    sub.add_parser("logout", help="Выйти из аккаунта").set_defaults(func=cmd_logout)
    sub.add_parser("projects", help="Список твоих проектов").set_defaults(func=cmd_projects)

    p_create = sub.add_parser("create", help="Создать новый проект")
    p_create.add_argument("slug", help="Поддомен проекта, например my-bot")
    p_create.add_argument("--kind", choices=["site", "bot", "backend"], default="backend")
    p_create.set_defaults(func=cmd_create)

    p_deploy = sub.add_parser("deploy", help="Задеплоить текущую папку")
    p_deploy.add_argument("slug", help="Slug проекта (см. python3 verf.py projects)")
    p_deploy.set_defaults(func=cmd_deploy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
