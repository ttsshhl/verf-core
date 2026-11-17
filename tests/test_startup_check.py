import json
import pytest

from app import deployer, diagnose, monitor, config
from app.models import DeployStatus


# ---------- diagnose ----------

@pytest.mark.parametrize("log,expect", [
    ("Токен не задан. Установите переменную окружения FUNPAY_BOT_TOKEN", "переменные окружения"),
    ("Не удалось запустить бота: Заполните TELEGRAM_BOT_TOKEN в .env.", "переменные окружения"),
    ("aiogram.utils.token.TokenValidationError: Token is invalid! It can't contains spaces.", "лишним пробелом"),
    ("aiogram.exceptions.TelegramUnauthorizedError: Telegram server says - Unauthorized", "@BotFather"),
    ("  import json, time, logging, msvcrt\nModuleNotFoundError: No module named 'msvcrt'", "Windows"),
    ("ModuleNotFoundError: No module named 'aiogram'", "requirements.txt"),
    ("KeyError: 'DATABASE_URL'", "DATABASE_URL"),
    ("Error: Cannot find module 'telegraf'", "package.json"),
])
def test_hints_for_common_crashes(log, expect):
    assert expect in diagnose.hint_for(log)


def test_no_hint_for_unknown_error():
    assert diagnose.hint_for("something weird happened") is None
    assert diagnose.hint_for("") is None


# ---------- monitor: auto-stop of crash loops ----------

def _snap(containers):
    return {"mem_pct": 10, "swap_pct": 0, "disk_pct": 10, "mem_available_mb": 3000, "containers": containers, "infra": []}


def _c(slug, status="restarting", code=1):
    return {"slug": slug, "owner": "o@x.ru", "status": status, "exit_code": code, "oom": False, "restart_count": 5, "mem_limit_mb": 256}


def test_crash_loop_is_queued_for_stop_after_threshold(monkeypatch):
    monkeypatch.setattr(config, "CRASHLOOP_STOP_MINUTES", 30)
    st = monitor.MonitorState()
    for t in range(0, 29 * 60, 60):
        monitor.evaluate(_snap([_c("bad")]), st, now=t)
    assert st.actions == []                       # not yet 30 minutes
    monitor.evaluate(_snap([_c("bad")]), st, now=31 * 60)
    assert st.actions == [("bad", "o@x.ru")]
    monitor.evaluate(_snap([_c("bad")]), st, now=32 * 60)
    assert len(st.actions) == 1                   # queued once only


def test_short_loop_that_recovers_is_never_stopped(monkeypatch):
    monkeypatch.setattr(config, "CRASHLOOP_STOP_MINUTES", 30)
    st = monitor.MonitorState()
    for t in range(0, 10 * 60, 60):
        monitor.evaluate(_snap([_c("flaky")]), st, now=t)
    for t in range(10 * 60, 20 * 60, 60):         # recovered: stays running
        monitor.evaluate(_snap([_c("flaky", "running", 0)]), st, now=t)
    monitor.evaluate(_snap([_c("flaky")]), st, now=40 * 60)   # a later, new loop starts its own clock
    assert st.actions == []


def test_single_running_flicker_does_not_reset_the_clock(monkeypatch):
    monkeypatch.setattr(config, "CRASHLOOP_STOP_MINUTES", 30)
    st = monitor.MonitorState()
    for t in range(0, 31 * 60, 60):
        status = "running" if t == 15 * 60 else "restarting"   # one lucky poll between crashes
        monitor.evaluate(_snap([_c("bad", status)]), st, now=t)
    assert st.actions == [("bad", "o@x.ru")]


def test_autostop_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "CRASHLOOP_STOP_MINUTES", 0)
    st = monitor.MonitorState()
    for t in range(0, 3 * 3600, 60):
        monitor.evaluate(_snap([_c("bad")]), st, now=t)
    assert st.actions == []


def test_stopped_project_does_not_also_raise_a_down_alert(monkeypatch):
    monkeypatch.setattr(config, "CRASHLOOP_STOP_MINUTES", 30)
    st = monitor.MonitorState()
    for t in range(0, 32 * 60, 60):
        monitor.evaluate(_snap([_c("bad")]), st, now=t)
    out = []
    for t in range(33 * 60, 36 * 60, 60):
        out += monitor.evaluate(_snap([_c("bad", "exited", 1)]), st, now=t)
    assert not any("остановлен, код выхода" in m for m in out)


def test_autostop_stops_emails_owner_and_reports(monkeypatch):
    from app import notifications
    seen = {}
    monkeypatch.setattr(deployer, "container_log_tail", lambda slug, n=20: "Токен не задан. Установите TELEGRAM_BOT_TOKEN")
    monkeypatch.setattr(deployer, "stop_crashing", lambda slug: seen.update(stopped=slug) or True)
    monkeypatch.setattr(notifications, "send_project_crashed_email", lambda to, slug, tail, hint: seen.update(mail=(to, slug, hint)))
    line = monitor.autostop("bad", "owner@x.ru")
    assert seen["stopped"] == "bad" and seen["mail"][0] == "owner@x.ru" and "переменные" in seen["mail"][2]
    assert "Остановлен bad" in line and "письмо" in line


def test_autostop_survives_mail_failure(monkeypatch):
    from app import notifications
    monkeypatch.setattr(deployer, "container_log_tail", lambda slug, n=20: "")
    monkeypatch.setattr(deployer, "stop_crashing", lambda slug: True)

    def boom(*a):
        raise notifications.EmailError("smtp down")
    monkeypatch.setattr(notifications, "send_project_crashed_email", boom)
    assert "письмо не ушло" in monitor.autostop("bad", "o@x.ru")


def test_crash_email_escapes_log_content(monkeypatch):
    from app import notifications
    sent = {}
    monkeypatch.setattr(notifications, "send_email", lambda to, subj, html: sent.update(html=html))
    notifications.send_project_crashed_email("o@x.ru", "bad", "<script>alert(1)</script>", "hint <b>")
    assert "<script>" not in sent["html"] and "&lt;script&gt;" in sent["html"]
