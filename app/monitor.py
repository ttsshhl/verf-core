"""Server health monitor — runs inside the API process and pings the admin's
Telegram (the alerts bot) when something needs attention:

  * host RAM / swap / disk above a threshold,
  * a customer container that is down, killed for exceeding its memory limit
    (OOM), or stuck in a restart loop,
  * a core service (Traefik, docker-proxy) that is not running,
  * this API itself having just (re)started.

Every problem alerts ONCE when it appears (after two consecutive bad checks, so
a deploy or a blip doesn't page you), says so again when it clears, and
repeats as a reminder only every REMIND_AFTER seconds while it persists.

An alert about the API being down cannot come from the API itself — for that
keep an external uptime check (e.g. UptimeRobot on api.verfdeploy.ru).

Snapshot of the current state without waiting:  python -m app.monitor
"""
import asyncio
import shutil
import time

from app import alerts, config

CONFIRM_PASSES = 2          # consecutive bad checks before the first alert
REMIND_AFTER = 6 * 3600     # re-alert interval while a problem persists
RESTART_WINDOW = 600        # seconds: restart-loop = RESTART_BURST restarts inside this window
RESTART_BURST = 3
RECOVER_MARGIN = 5          # a % metric must drop this far below its threshold to count as recovered


# ---------- collecting ----------

def _meminfo() -> dict:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])  # kB
    return info


def host_metrics() -> dict:
    mi = _meminfo()
    total = mi.get("MemTotal", 0)
    mem_pct = round((1 - mi.get("MemAvailable", total) / total) * 100) if total else 0
    swap_total = mi.get("SwapTotal", 0)
    swap_pct = round((1 - mi.get("SwapFree", 0) / swap_total) * 100) if swap_total else 0
    du = shutil.disk_usage("/")
    return {
        "mem_pct": mem_pct,
        "swap_pct": swap_pct,
        "disk_pct": round(du.used / du.total * 100),
        "mem_available_mb": mi.get("MemAvailable", 0) // 1024,
    }


def docker_state(slug_owner: dict) -> dict:
    """Containers of user projects + core compose services.
    `slug_owner` maps project slug -> owner email (for readable alerts)."""
    from app import deployer
    client = deployer._client()
    containers, infra = [], []
    for c in client.containers.list(all=True):
        name = c.name
        labels = c.labels or {}
        attrs = c.attrs or {}
        state = attrs.get("State", {})
        if labels.get("com.docker.compose.project") == "verf-core" and labels.get("com.docker.compose.service") in ("traefik", "docker-proxy", "verf-core"):
            infra.append({"name": labels["com.docker.compose.service"], "running": c.status == "running"})
            continue
        if not name.startswith("verf-") or name.endswith("-old"):
            continue
        slug = name[len("verf-"):]
        if slug not in slug_owner:
            continue
        containers.append({
            "slug": slug,
            "owner": slug_owner[slug],
            "status": c.status,
            "exit_code": state.get("ExitCode"),
            "oom": bool(state.get("OOMKilled")),
            "restart_count": attrs.get("RestartCount", 0),
            "mem_limit_mb": (attrs.get("HostConfig", {}).get("Memory") or 0) // (1024 * 1024),
            "policy": (attrs.get("HostConfig", {}).get("RestartPolicy") or {}).get("Name", ""),
        })
    return {"containers": containers, "infra": infra}


def collect() -> dict:
    from app.db import SessionLocal
    from app.models import Project

    snap = host_metrics()
    db = SessionLocal()
    try:
        slug_owner = {p.slug: (p.owner.email if p.owner else "—") for p in db.query(Project).all()}
    finally:
        db.close()
    try:
        snap.update(docker_state(slug_owner))
    except Exception as exc:
        snap["docker_error"] = str(exc)
        snap.update(containers=[], infra=[])
    return snap


# ---------- evaluating (pure, unit-tested) ----------

class MonitorState:
    def __init__(self):
        self.bad_passes: dict[str, int] = {}     # problem key -> consecutive bad checks
        self.alerted_at: dict[str, float] = {}   # problem key -> when we last alerted
        self.restarts: dict[str, list] = {}      # slug -> [(ts, restart_count)]
        self.labels: dict[str, str] = {}         # problem key -> short name for the recovery message
        self.loop_since: dict[str, float] = {}   # slug -> when it first looked crash-looping
        self.loop_ok_passes: dict[str, int] = {} # slug -> consecutive checks without looping (flicker tolerance)
        self.autostopped: set[str] = set()       # slugs we already stopped (until they run again)
        self.actions: list[tuple[str, str]] = [] # (slug, owner) to auto-stop, drained by the loop


def _problems(snap: dict, state: MonitorState, now: float) -> dict[str, str]:
    """Returns {key: alert text} for every currently-bad thing; also records
    a short label per key for the later "back to normal" message."""
    bad = {}

    def pct(key, label, value, limit, extra=""):
        # still inside the hysteresis band counts as bad once we have alerted
        if value >= limit or (value >= limit - RECOVER_MARGIN and key in state.alerted_at):
            bad[key] = f"⚠️ {label}: {value}% (порог {limit}%){extra}"
            state.labels[key] = label.lower()

    pct("mem", "Память", snap.get("mem_pct", 0), config.MONITOR_MEM_PCT,
        f" — свободно ~{snap.get('mem_available_mb', 0)} МБ. Посмотри `docker stats --no-stream`")
    # A full swap with plenty of free RAM is just old idle pages parked there —
    # not an incident. It matters only when memory is tight as well.
    if snap.get("mem_pct", 0) >= config.MONITOR_MEM_PCT - 15 or "swap" in state.alerted_at:
        pct("swap", "Swap", snap.get("swap_pct", 0), config.MONITOR_SWAP_PCT, " — RAM на исходе, система свопится")
    pct("disk", "Диск", snap.get("disk_pct", 0), config.MONITOR_DISK_PCT, " — почисти `docker system prune -af`")

    if snap.get("docker_error"):
        bad["docker"] = f"🔴 Мониторинг не видит Docker: {snap['docker_error'][:150]}"
        state.labels["docker"] = "доступ к Docker"

    for svc in snap.get("infra", []):
        if not svc["running"]:
            bad[f"infra:{svc['name']}"] = f"🔴 Сервис платформы не запущен: {svc['name']}"
            state.labels[f"infra:{svc['name']}"] = f"сервис {svc['name']}"

    for c in snap.get("containers", []):
        slug, owner = c["slug"], c["owner"]
        hist = state.restarts.setdefault(slug, [])
        hist.append((now, c["restart_count"]))
        state.restarts[slug] = hist = [(t, n) for t, n in hist if now - t <= RESTART_WINDOW]
        burst = c["restart_count"] - hist[0][1]

        if c["oom"]:
            bad[f"oom:{slug}"] = (f"🔴 {slug} ({owner}) убит за превышение памяти — лимит {c['mem_limit_mb']} МБ. "
                                  f"Клиенту нужно больше памяти или оптимизация бота")
        if c["status"] == "restarting" or burst >= RESTART_BURST:
            how = f"{burst} раз за {RESTART_WINDOW // 60} мин" if burst >= RESTART_BURST else f"код выхода {c['exit_code']}"
            bad[f"loop:{slug}"] = (f"🔴 {slug} ({owner}) падает и перезапускается по кругу "
                                   f"({how}). Логи: `docker logs --tail 50 verf-{slug}`")
        elif (c["status"] in ("exited", "dead") and slug not in state.autostopped
              and c.get("policy", "no") not in ("unless-stopped", "always")):
            # With restart policy unless-stopped/always Docker brings a crashed
            # container straight back (we'd see "restarting"), so a container
            # that stays "exited" was stopped ON PURPOSE — by the admin, by a
            # lapsed plan, or by our own auto-stop — and is not an incident.
            bad[f"down:{slug}"] = (f"🔴 {slug} ({owner}) остановлен, код выхода {c['exit_code']}. "
                                   f"Логи: `docker logs --tail 50 verf-{slug}`")
    for key in bad:
        if key.split(":")[0] in ("oom", "loop", "down"):
            state.labels[key] = key.split(":", 1)[1]
    return bad


def _track_crash_loops(snap: dict, bad: dict, state: MonitorState, now: float) -> None:
    """A project that keeps crash-looping past CRASHLOOP_STOP_MINUTES gets
    queued for an automatic stop (the loop does the docker/email part)."""
    looping = {k.split(":", 1)[1] for k in bad if k.startswith("loop:")}
    owners = {c["slug"]: c["owner"] for c in snap.get("containers", [])}
    running = {c["slug"] for c in snap.get("containers", []) if c["status"] == "running"}

    for slug in looping:
        state.loop_since.setdefault(slug, now)
        state.loop_ok_passes[slug] = 0
    for slug in list(state.loop_since):
        if slug in looping:
            continue
        state.loop_ok_passes[slug] = state.loop_ok_passes.get(slug, 0) + 1
        if state.loop_ok_passes[slug] >= 3 or slug not in owners:   # really recovered, or deleted
            state.loop_since.pop(slug, None)
            state.loop_ok_passes.pop(slug, None)
    for slug in list(state.autostopped):
        if slug in running and slug not in looping:   # the customer redeployed and it stays up
            state.autostopped.discard(slug)

    limit = config.CRASHLOOP_STOP_MINUTES * 60
    if limit <= 0:
        return
    for slug, since in state.loop_since.items():
        if now - since >= limit and slug not in state.autostopped and slug in owners:
            state.autostopped.add(slug)
            state.actions.append((slug, owners[slug]))


def evaluate(snap: dict, state: MonitorState, now: float | None = None) -> list[str]:
    """Pure state machine: snapshot in, list of messages to send out."""
    now = time.time() if now is None else now
    bad = _problems(snap, state, now)
    out = []

    for key, text in bad.items():
        state.bad_passes[key] = state.bad_passes.get(key, 0) + 1
        if state.bad_passes[key] < CONFIRM_PASSES:
            continue
        last = state.alerted_at.get(key)
        if last is None:
            out.append(text)
            state.alerted_at[key] = now
        elif now - last >= REMIND_AFTER:
            out.append("🔁 Всё ещё актуально: " + text)
            state.alerted_at[key] = now

    _track_crash_loops(snap, bad, state, now)

    for key in list(state.bad_passes):
        if key in bad:
            continue
        state.bad_passes.pop(key, None)
        if key in state.alerted_at:
            state.alerted_at.pop(key)
            out.append(f"✅ Снова в порядке: {state.labels.pop(key, key)}")

    # forget history of deleted projects
    live = {c["slug"] for c in snap.get("containers", [])}
    for slug in list(state.restarts):
        if slug not in live:
            state.restarts.pop(slug)
    return out


# ---------- running ----------

def autostop(slug: str, owner: str) -> str:
    """Stops a project that has been crash-looping too long, emails its owner
    the reason, and returns the admin-facing line."""
    from app import deployer, diagnose, notifications
    tail = deployer.container_log_tail(slug, 20)
    hint = diagnose.hint_for(tail)
    deployer.stop_crashing(slug)
    mailed = ""
    if owner and "@" in owner:
        try:
            notifications.send_project_crashed_email(owner, slug, tail, hint)
            mailed = ", владельцу отправлено письмо"
        except notifications.EmailError as exc:
            mailed = f", письмо не ушло ({exc})"
    why = f" Причина: {hint}" if hint else ""
    return f"⛔ Остановлен {slug} ({owner}): падал по кругу {config.CRASHLOOP_STOP_MINUTES}+ мин{mailed}.{why}"


def digest(texts: list[str], limit: int = 3500) -> str:
    """Joins several alerts into one Telegram message (4096-char cap)."""
    if len(texts) == 1:
        return texts[0]
    out = f"Проблем: {len(texts)}\n\n" + "\n\n".join(texts)
    return out if len(out) <= limit else out[:limit].rsplit("\n\n", 1)[0] + "\n\n… и другие, см. `python -m app.monitor`"


def _send(text: str) -> None:
    try:
        alerts.send_admin_alert(text)
    except alerts.AlertError as exc:
        print(f"[monitor] оповещение не отправлено: {exc}")


async def monitor_loop(interval: int) -> None:
    """Runs forever inside the API process. Never raises."""
    state = MonitorState()
    await asyncio.to_thread(_send, "🔄 VERF API запущен (если вы не деплоили платформу — сервис перезапускался сам)")
    while True:
        await asyncio.sleep(interval)
        try:
            snap = await asyncio.to_thread(collect)
            texts = evaluate(snap, state)
            while state.actions:
                slug, owner = state.actions.pop(0)
                texts.append(await asyncio.to_thread(autostop, slug, owner))
            if texts:
                # One message per check, not one per problem: a host-wide event
                # (memory pressure, docker restart) hits many projects at once.
                await asyncio.to_thread(_send, digest(texts))
        except Exception as exc:
            print(f"[monitor] ошибка проверки: {exc}")


def main() -> None:
    import json
    print(json.dumps(collect(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
