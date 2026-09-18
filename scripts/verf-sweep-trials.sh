#!/bin/bash
# VERF — ежедневная проверка пробного периода. Вызывает admin-эндпоинт,
# который сам находит истёкшие Free-аккаунты и приостанавливает их
# контейнеры (не удаляя — апгрейд мгновенно всё возвращает).
#
# Настройка — впиши свой admin-ключ (тот же, что VERF_ADMIN_API_KEY в .env):
ADMIN_API_KEY=""

API_URL="https://api.verfdeploy.ru/admin/sweep-expired-trials"

if [ -z "$ADMIN_API_KEY" ]; then
  echo "ADMIN_API_KEY не задан — впиши его в начале скрипта" >&2
  exit 1
fi

curl -s -X POST "$API_URL" -H "X-API-Key: ${ADMIN_API_KEY}"
echo
