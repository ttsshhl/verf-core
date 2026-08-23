"""Docker build + run layer.

Requires a Docker daemon (unix:///var/run/docker.sock by default — the
`docker` package picks this up automatically via docker.from_env()).
Not exercised by the test suite in an environment without Docker; the
webhook/builder logic above it is fully covered instead, and this module
is kept thin so the untestable part is as small as possible.
"""
from app.builder import project_dir
from app.config import (
    DOCKER_NETWORK,
    DOMAIN_SUFFIX,
    TRAEFIK_ENTRYPOINT,
    TRAEFIK_CERTRESOLVER,
    DEFAULT_MEM_LIMIT,
    DEFAULT_CPU_QUOTA,
)


class DeployError(Exception):
    pass


def _client():
    import docker  # imported lazily so the rest of the app works without the docker package/daemon present

    try:
        return docker.from_env()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise DeployError(f"Не удалось подключиться к Docker: {exc}")


def ensure_network() -> None:
    client = _client()
    existing = [n for n in client.networks.list() if n.name == DOCKER_NETWORK]
    if not existing:
        client.networks.create(DOCKER_NETWORK, driver="bridge")


def build_image(slug: str, deployment_id: str) -> str:
    client = _client()
    image_tag = f"verf/{slug}:{deployment_id}"
    root = project_dir(slug)
    try:
        _, log_stream = client.images.build(path=str(root), tag=image_tag, rm=True)
        for chunk in log_stream:
            # chunk is a dict like {"stream": "..."} — caller can persist this if desired
            pass
    except Exception as exc:  # docker.errors.BuildError, APIError, etc.
        raise DeployError(f"Сборка образа не удалась: {exc}")
    return image_tag


def _labels(slug: str) -> dict:
    router = f"verf-{slug}"
    host_rule = f"Host(`{slug}.{DOMAIN_SUFFIX}`)"
    return {
        "traefik.enable": "true",
        f"traefik.http.routers.{router}.rule": host_rule,
        f"traefik.http.routers.{router}.entrypoints": TRAEFIK_ENTRYPOINT,
        f"traefik.http.routers.{router}.tls.certresolver": TRAEFIK_CERTRESOLVER,
    }


def run_container(slug: str, image_tag: str, internal_port: int, env: dict | None = None):
    """Start the new container, then stop+remove any previous one for this slug.

    New-then-old ordering keeps the old version answering traffic until the
    new one is confirmed running — a minimal blue/green swap.
    """
    client = _client()
    ensure_network()

    container_name = f"verf-{slug}"
    old = None
    try:
        old = client.containers.get(container_name)
    except Exception:
        pass  # no previous deployment — fine

    if old is not None:
        old.rename(f"{container_name}-old")

    try:
        new = client.containers.run(
            image_tag,
            name=container_name,
            detach=True,
            network=DOCKER_NETWORK,
            environment=env or {},
            labels=_labels(slug),
            mem_limit=DEFAULT_MEM_LIMIT,
            cpu_period=100_000,
            cpu_quota=DEFAULT_CPU_QUOTA,
            restart_policy={"Name": "unless-stopped"},
        )
    except Exception as exc:
        if old is not None:
            old.rename(container_name)  # roll back the rename so old keeps serving traffic
        raise DeployError(f"Запуск контейнера не удался: {exc}")

    if old is not None:
        old.stop(timeout=10)
        old.remove()

    return new.id


def stop_and_remove(slug: str) -> None:
    client = _client()
    container_name = f"verf-{slug}"
    try:
        c = client.containers.get(container_name)
        c.stop(timeout=10)
        c.remove()
    except Exception:
        pass  # already gone — deleting a project should be idempotent
