# VERF core — ядро деплоя

GitHub push → сборка образа → запуск контейнера → домен вида `slug.verf.dev` с автоматическим SSL.

Это MVP-ядро: одна нода, без оркестрации кластера. Рассчитано на один VPS, поднимающий десятки проектов — этого достаточно для первых сотен пользователей.

## Что уже работает (покрыто тестами)

- Проверка подписи GitHub-вебхука (HMAC-SHA256) — 6 тестов
- Клонирование/обновление репозитория, определение типа проекта (Node / Python / статика / свой Dockerfile), автогенерация Dockerfile — 9 тестов
- REST API: создание/список/удаление проектов, приём вебхука, история деплоев — 9 тестов
- Полный пайплайн деплоя, включая обработку ошибок и запись логов в БД

Docker-слой (`app/deployer.py`) написан на официальном `docker` SDK и рассчитан на реальный Docker-демон — у меня в песочнице его нет физически, поэтому этот файл не покрыт тестами напрямую, но весь пайплайн до и после него — да.

```
pytest tests/ -v    # 26 passed
```

## Архитектура

```
GitHub push
     │
     ▼
POST /webhook/github/{slug}   ── проверка HMAC-подписи (app/webhook.py)
     │
     ▼
Deployment(status=pending) записан в БД
     │  (ответ GitHub'у уходит сразу — сборка идёт в фоне)
     ▼
app/pipeline.py: run_deploy()
     │
     ├─ builder.clone_or_pull()      git clone/pull, возвращает commit SHA
     ├─ builder.detect_profile()     ищет Dockerfile → package.json → requirements.txt → index.html
     ├─ builder.ensure_dockerfile()  генерирует Dockerfile, если репозиторий пришёл без него
     ├─ deployer.build_image()       docker build
     └─ deployer.run_container()     останавливает старый контейнер, запускает новый с лейблами Traefik
```

Traefik читает Docker-лейблы каждого запущенного контейнера и сам настраивает роутинг + SSL — в `app/deployer.py` ничего вручную не прописывается.

## Запуск на VPS (Timeweb Cloud / Selectel / любой с Ubuntu 22.04+)

1. **Установить Docker и Docker Compose**
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

2. **Направить домен на сервер** — A-запись `*.verf.dev` (или твой домен) → IP VPS. Wildcard-запись нужна, чтобы `любой-slug.verf.dev` резолвился без ручной настройки DNS под каждый проект.

3. **Склонировать этот репозиторий на сервер и настроить `.env`**
   ```bash
   cp .env.example .env
   # заполнить ACME_EMAIL, VERF_DOMAIN_SUFFIX, VERF_ADMIN_API_KEY
   ```

4. **Поднять стек**
   ```bash
   docker network create verf-net || true
   docker compose up -d --build
   ```

5. **Проверить, что ядро живо**
   ```bash
   curl http://127.0.0.1:8000/health
   # {"status": "ok"}
   ```

## Как задеплоить первый проект

1. **Зарегистрировать проект** (с VPS или через SSH-туннель на порт 8000, т.к. `/projects` не публичный):
   ```bash
   curl -X POST http://127.0.0.1:8000/projects \
     -H "X-API-Key: $VERF_ADMIN_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "slug": "my-bot",
       "repo_url": "https://github.com/you/my-bot.git",
       "branch": "main",
       "kind": "bot"
     }'
   ```
   В ответе будет `webhook_secret` — он нужен на следующем шаге.

2. **Добавить вебхук в GitHub** (Settings → Webhooks → Add webhook):
   - Payload URL: `https://твой-домен/webhook/github/my-bot` *(проксируется через Traefik — добавь лейблы на verf-core в compose, если хочешь публичный доступ к вебхуку без открытого порта 8000; в MVP-конфиге вебхук пока идёт напрямую на 8000, см. "Известные ограничения")*
   - Content type: `application/json`
   - Secret: значение `webhook_secret` из шага 1
   - Событие: `Just the push event`

3. **Пушнуть в репозиторий** — GitHub дёрнет вебхук, VERF соберёт образ и поднимет контейнер на `https://my-bot.verf.dev`.

4. **Проверить статус деплоя**
   ```bash
   curl http://127.0.0.1:8000/projects/my-bot/deployments \
     -H "X-API-Key: $VERF_ADMIN_API_KEY"
   ```

## Известные ограничения MVP (осознанно, чтобы не блокировать запуск)

- **Порт 8000 не публикуется через Traefik по умолчанию.** Для приёма вебхуков GitHub тебе нужен публичный HTTPS-адрес — либо добавь Traefik-лейблы на сервис `verf-core` в `docker-compose.yml` (аналогично тому, что `deployer.py` делает для пользовательских контейнеров), либо на первое время используй `ngrok`/SSH-туннель для теста.
- **Один VPS, без очереди задач.** Деплой идёт в `BackgroundTasks` внутри процесса FastAPI — нормально для десятков проектов, но если одновременных пушей станет много, следующий шаг — вынести `run_deploy` в Celery/RQ с отдельным воркером.
- **SQLite.** Хватает для одного узла. Переезд на Postgres — это просто смена `VERF_DATABASE_URL`, код не завязан на конкретную БД.
- **Нет личного кабинета и биллинга** — это следующий кусок в очереди, ты сам так решил на предыдущем шаге.
