"""Turns a crashed project's last log lines into a plain-language hint.

Most failed starts on a PaaS are not platform problems but a missing/wrong
environment variable or dependency; saying so directly saves the customer
(and the admin) a support round-trip.
"""
import re

_ENV_HINT = ("Похоже, не заданы обязательные переменные окружения (токен, ключ или пароль). "
             "Добавьте их в настройках проекта → «Переменные» и перезапустите.")

_RULES = [
    (r"KeyError: ['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
     lambda m: f"Не задана переменная окружения {m.group(1)}. Добавьте её в настройках проекта → «Переменные» и перезапустите."),
    (r"TokenValidationError|Token is invalid",
     lambda m: "Токен бота неверный (возможно, с лишним пробелом или переносом строки). Вставьте токен из @BotFather без пробелов."),
    (r"Unauthorized|401: Unauthorized|Invalid token|ETELEGRAM: 401",
     lambda m: "Telegram отклонил токен: он недействителен или отозван. Получите новый у @BotFather и обновите переменную."),
    (r"(токен|token|api[_ ]?key|секрет|secret|password|пароль)[^\n]{0,60}(не задан|не указан|не найден|не установлен|not set|is required|missing|empty|Заполните)",
     lambda m: _ENV_HINT),
    (r"(Заполните|Установите|Укажите)[^\n]{0,60}(TOKEN|KEY|PASSWORD|\.env|переменн)",
     lambda m: _ENV_HINT),
    (r"No module named '(msvcrt|winreg|_winapi|win32\w*)'",
     lambda m: f"Код использует Windows-модуль «{m.group(1)}», на Linux-сервере он недоступен. Уберите Windows-специфичный код."),
    (r"ModuleNotFoundError: No module named '([\w\.]+)'",
     lambda m: f"Не найден Python-модуль «{m.group(1)}». Добавьте его в requirements.txt и задеплойте заново."),
    (r"Cannot find module '([^']+)'",
     lambda m: f"Не найден Node-модуль «{m.group(1)}». Проверьте package.json (dependencies) и задеплойте заново."),
    (r"EADDRINUSE|address already in use",
     lambda m: "Порт уже занят внутри контейнера. Запускайте только один процесс на порту."),
    (r"ECONNREFUSED|ConnectionRefusedError|Connection refused",
     lambda m: "Проект не смог подключиться к внешнему сервису (база данных, API). Проверьте адрес и доступность."),
    (r"No such file or directory: '([^']+)'|ENOENT[^\n]*'([^']+)'",
     lambda m: f"Не найден файл «{m.group(1) or m.group(2)}». Проверьте, что он есть в репозитории."),
]


def hint_for(log_text: str) -> str | None:
    for pattern, make in _RULES:
        m = re.search(pattern, log_text or "", re.IGNORECASE)
        if m:
            return make(m)
    return None
