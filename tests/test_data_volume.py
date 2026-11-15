from app import deployer


class _FakeContainers:
    def __init__(self):
        self.run_kwargs = None
    def get(self, name):
        raise Exception("not found")
    def run(self, image, **kw):
        self.run_kwargs = kw
        class C: id = "cid"
        return C()


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers()


def test_run_container_mounts_persistent_data_volume(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(deployer, "_client", lambda: fake)
    monkeypatch.setattr(deployer, "ensure_network", lambda: None)
    deployer.run_container("my-bot", "img:1", 8080, {"BOT_TOKEN": "x"})
    kw = fake.containers.run_kwargs
    assert kw["volumes"] == {"verf-data-my-bot": {"bind": "/data", "mode": "rw"}}
    assert kw["environment"]["DATA_DIR"] == "/data"
    assert kw["environment"]["BOT_TOKEN"] == "x"


def test_user_can_override_data_dir_env(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(deployer, "_client", lambda: fake)
    monkeypatch.setattr(deployer, "ensure_network", lambda: None)
    deployer.run_container("my-bot", "img:1", 8080, {"DATA_DIR": "/custom"})
    assert fake.containers.run_kwargs["environment"]["DATA_DIR"] == "/custom"


def test_stop_and_remove_drops_the_data_volume(monkeypatch):
    removed = []
    class Vol:
        def remove(self, force=False): removed.append(force)
    class Vols:
        def get(self, name):
            assert name == "verf-data-my-bot"
            return Vol()
    class Containers:
        def get(self, name): raise Exception("gone")
    class Client:
        containers = Containers(); volumes = Vols()
    monkeypatch.setattr(deployer, "_client", lambda: Client())
    deployer.stop_and_remove("my-bot")
    assert removed == [True]


def test_user_container_cannot_use_host_swap(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(deployer, "_client", lambda: fake)
    monkeypatch.setattr(deployer, "ensure_network", lambda: None)
    deployer.run_container("my-bot", "img:1", 8080, {}, mem_limit="256m")
    kw = fake.containers.run_kwargs
    assert kw["mem_limit"] == "256m" and kw["memswap_limit"] == "256m"


def test_update_limits_applies_in_place(monkeypatch):
    calls = {}
    class C:
        def update(self, **kw): calls.update(kw)
    class Containers:
        def get(self, name):
            assert name == "verf-my-bot"
            return C()
    class Client:
        containers = Containers()
    monkeypatch.setattr(deployer, "_client", lambda: Client())
    assert deployer.update_limits("my-bot", "1g", 100_000) is True
    assert calls == {"mem_limit": "1g", "memswap_limit": "1g", "cpu_period": 100_000, "cpu_quota": 100_000}
