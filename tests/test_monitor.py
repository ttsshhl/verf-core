import pytest

from app import monitor, config


def snap(mem=50, swap=0, disk=40, containers=None, infra=None):
    return {"mem_pct": mem, "swap_pct": swap, "disk_pct": disk, "mem_available_mb": 300,
            "containers": containers or [], "infra": infra or []}


def cont(slug="bot", status="running", exit_code=0, oom=False, restarts=0):
    return {"slug": slug, "owner": "u@x.ru", "status": status, "exit_code": exit_code,
            "oom": oom, "restart_count": restarts, "mem_limit_mb": 256}


def run(state, s, t):
    return monitor.evaluate(s, state, now=t)


def test_healthy_server_is_silent():
    st = monitor.MonitorState()
    assert run(st, snap(containers=[cont()]), 0) == []


def test_memory_alert_needs_two_checks_then_fires_once_then_recovers():
    st = monitor.MonitorState()
    assert run(st, snap(mem=95), 0) == []            # first bad check: wait
    out = run(st, snap(mem=95), 60)
    assert len(out) == 1 and "Память" in out[0] and "95%" in out[0]
    assert run(st, snap(mem=96), 120) == []          # no repeat spam
    assert run(st, snap(mem=88), 180) == []          # inside hysteresis band: still not "recovered"
    out = run(st, snap(mem=60), 240)
    assert out == ["✅ Снова в порядке: память"]


def test_reminder_after_six_hours():
    st = monitor.MonitorState()
    run(st, snap(disk=90), 0); run(st, snap(disk=90), 60)
    assert run(st, snap(disk=90), 3600) == []
    out = run(st, snap(disk=90), 60 + 6 * 3600)
    assert len(out) == 1 and out[0].startswith("🔁")


def test_blip_does_not_alert():
    st = monitor.MonitorState()
    run(st, snap(mem=95), 0)
    assert run(st, snap(mem=50), 60) == []
    assert run(st, snap(mem=95), 120) == []  # counter restarted


def test_stopped_container_alerts_with_owner_and_exit_code():
    st = monitor.MonitorState()
    bad = snap(containers=[cont("salon", "exited", 1)])
    run(st, bad, 0)
    out = run(st, bad, 60)
    assert len(out) == 1 and "salon" in out[0] and "u@x.ru" in out[0] and "код выхода 1" in out[0]
    assert run(st, snap(containers=[cont("salon")]), 120) == ["✅ Снова в порядке: salon"]


def test_oom_kill_is_reported_with_limit():
    st = monitor.MonitorState()
    s = snap(containers=[cont("hog", "running", 137, oom=True)])
    run(st, s, 0)
    out = run(st, s, 60)
    assert "превышение памяти" in out[0] and "256 МБ" in out[0]


def test_restart_loop_detected():
    st = monitor.MonitorState()
    run(st, snap(containers=[cont("loopy", restarts=0)]), 0)
    run(st, snap(containers=[cont("loopy", restarts=2)]), 60)
    run(st, snap(containers=[cont("loopy", restarts=4)]), 120)   # first bad check: wait
    out = run(st, snap(containers=[cont("loopy", restarts=5)]), 180)
    assert any("по кругу" in m and "loopy" in m for m in out)


def test_infra_service_down():
    st = monitor.MonitorState()
    s = snap(infra=[{"name": "traefik", "running": False}])
    run(st, s, 0)
    out = run(st, s, 60)
    assert out and "traefik" in out[0]


def test_deleted_project_history_forgotten():
    st = monitor.MonitorState()
    run(st, snap(containers=[cont("gone")]), 0)
    run(st, snap(), 60)
    assert "gone" not in st.restarts


def test_host_metrics_reads_real_proc():
    m = monitor.host_metrics()
    assert 0 <= m["mem_pct"] <= 100 and 0 <= m["disk_pct"] <= 100


def test_alert_send_is_best_effort(monkeypatch):
    from app import alerts

    def boom(text):
        raise alerts.AlertError("not configured")
    monkeypatch.setattr(alerts, "send_admin_alert", boom)
    monitor._send("x")  # must not raise


def test_docker_unreachable_is_reported():
    st = monitor.MonitorState()
    s = snap(); s["docker_error"] = "socket gone"
    run(st, s, 0)
    out = run(st, s, 60)
    assert out and "Docker" in out[0]


def test_digest_groups_many_alerts_into_one_message():
    assert monitor.digest(["a"]) == "a"
    out = monitor.digest([f"проблема {i}" for i in range(7)])
    assert out.startswith("Проблем: 7") and out.count("проблема") == 7
    huge = monitor.digest(["x" * 900] * 10)
    assert len(huge) < 4096 and huge.endswith("python -m app.monitor`")


def test_loop_alert_for_restarting_status_shows_exit_code_not_fake_count():
    st = monitor.MonitorState()
    s = snap(containers=[cont("crashy", "restarting", 1)])
    run(st, s, 0)
    out = run(st, s, 60)
    assert "код выхода 1" in out[0] and "1 раз" not in out[0]
