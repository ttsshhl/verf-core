#!/bin/bash
# VERF ops monitor — проверяет память/swap/диск, шлёт оповещение в Telegram
# только при ПЕРЕСЕЧЕНИИ порога (не будет спамить каждый запуск, если
# состояние стабильно плохое или стабильно хорошее — только на переходах).
#
# Запускать на самом хосте (не в контейнере) через cron, см. инструкцию
# в README.
#
# Настройка — впиши свои значения:
BOT_TOKEN=""      # токен бота от @BotFather
CHAT_ID=""        # твой chat_id (см. инструкцию по получению)

MEM_THRESHOLD=85   # % использования оперативной памяти
SWAP_THRESHOLD=50  # % использования swap (рост swap — ранний признак нехватки RAM)
DISK_THRESHOLD=85  # % использования диска (корневой раздел)

STATE_FILE="/tmp/verf-monitor-state"

if [ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ]; then
  echo "BOT_TOKEN/CHAT_ID не заданы — впиши их в начале скрипта" >&2
  exit 1
fi

mem_pct=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2 * 100}')
swap_total=$(free | awk '/^Swap:/ {print $2}')
if [ "$swap_total" -gt 0 ]; then
  swap_pct=$(free | awk '/^Swap:/ {printf "%.0f", $3/$2 * 100}')
else
  swap_pct=0
fi
disk_pct=$(df / | awk 'NR==2 {gsub("%","",$5); print $5}')

send_telegram() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="$1" \
    > /dev/null
}

mem_alerted=0
swap_alerted=0
disk_alerted=0
[ -f "$STATE_FILE" ] && source "$STATE_FILE"

check_metric() {
  local name="$1" pct="$2" threshold="$3" alerted_var="$4"
  local was_alerted
  eval "was_alerted=\$${alerted_var}"
  if [ "$pct" -ge "$threshold" ] && [ "$was_alerted" -eq 0 ]; then
    send_telegram "⚠️ VERF: ${name} достигла ${pct}% (порог ${threshold}%)"
    eval "${alerted_var}=1"
  elif [ "$pct" -lt "$threshold" ] && [ "$was_alerted" -eq 1 ]; then
    send_telegram "✅ VERF: ${name} вернулась в норму (${pct}%)"
    eval "${alerted_var}=0"
  fi
}

check_metric "Память" "$mem_pct" "$MEM_THRESHOLD" mem_alerted
check_metric "Swap" "$swap_pct" "$SWAP_THRESHOLD" swap_alerted
check_metric "Диск" "$disk_pct" "$DISK_THRESHOLD" disk_alerted

cat > "$STATE_FILE" << EOF
mem_alerted=$mem_alerted
swap_alerted=$swap_alerted
disk_alerted=$disk_alerted
EOF
