import pytest


@pytest.fixture(autouse=True)
def default_healthy_container(monkeypatch):
    """deployer.wait_for_container_port() does a real TCP connect to the
    container's Docker-network hostname — meaningless in the test
    environment (no real Docker network to reach). Every test gets a
    "container is healthy" default so the dozens of existing tests that
    mock run_container and expect status=="running" keep working
    unchanged. Tests that specifically want to exercise the health-check
    failure path override this within their own test body — that
    monkeypatch.setattr call runs after this fixture's, so it wins.
    """
    from app import deployer
    monkeypatch.setattr(deployer, "wait_for_container_port", lambda slug, port, timeout=15.0: True)
