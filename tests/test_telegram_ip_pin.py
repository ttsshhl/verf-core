from app import deployer
from app.config import TELEGRAM_API_PINNED_IP


class FakeContainers:
    def __init__(self):
        self.run_calls = []

    def get(self, name):
        raise Exception("not found")  # no previous deployment — matches real docker SDK behaviour

    def run(self, image_tag, **kwargs):
        self.run_calls.append(kwargs)
        class FakeContainer:
            id = "fake-container-id-1234567890"
        return FakeContainer()


class FakeClient:
    def __init__(self):
        self.containers = FakeContainers()


def test_run_container_pins_telegram_api_ip(monkeypatch):
    fake_client = FakeClient()
    monkeypatch.setattr(deployer, "_client", lambda: fake_client)
    monkeypatch.setattr(deployer, "ensure_network", lambda: None)

    deployer.run_container("my-bot", "verf/my-bot:abc123", 8000, {})

    assert len(fake_client.containers.run_calls) == 1
    extra_hosts = fake_client.containers.run_calls[0]["extra_hosts"]
    assert extra_hosts == {"api.telegram.org": TELEGRAM_API_PINNED_IP}


def test_telegram_ip_pin_applies_to_every_project_regardless_of_kind(monkeypatch):
    """The whole point: this is a platform-wide fix, not something scoped
    to projects the user happened to label "bot" — extra_hosts entries a
    container never queries are harmless no-ops, so it's simplest and
    safest applied universally."""
    fake_client = FakeClient()
    monkeypatch.setattr(deployer, "_client", lambda: fake_client)
    monkeypatch.setattr(deployer, "ensure_network", lambda: None)

    deployer.run_container("a-plain-website", "verf/a-plain-website:abc123", 80, {})

    extra_hosts = fake_client.containers.run_calls[0]["extra_hosts"]
    assert extra_hosts == {"api.telegram.org": TELEGRAM_API_PINNED_IP}


def test_telegram_ip_pin_is_configurable_via_env(monkeypatch):
    """If this specific IP ever stops being reliable too, it should be a
    one-line .env change, not a code change."""
    monkeypatch.setattr(deployer, "TELEGRAM_API_PINNED_IP", "1.2.3.4")
    fake_client = FakeClient()
    monkeypatch.setattr(deployer, "_client", lambda: fake_client)
    monkeypatch.setattr(deployer, "ensure_network", lambda: None)

    deployer.run_container("my-bot", "verf/my-bot:abc123", 8000, {})

    extra_hosts = fake_client.containers.run_calls[0]["extra_hosts"]
    assert extra_hosts == {"api.telegram.org": "1.2.3.4"}
