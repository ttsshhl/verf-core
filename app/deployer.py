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
    TELEGRAM_API_PINNED_IP,
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


def _labels(slug: str, internal_port: int, custom_domain: str | None = None) -> dict:
    router = f"verf-{slug}"
    host_rule = f"Host(`{slug}.{DOMAIN_SUFFIX}`)"
    labels = {
        "traefik.enable": "true",
        f"traefik.http.routers.{router}.rule": host_rule,
        f"traefik.http.routers.{router}.entrypoints": TRAEFIK_ENTRYPOINT,
        f"traefik.http.routers.{router}.tls.certresolver": TRAEFIK_CERTRESOLVER,
        # Request a *wildcard* cert (*.verfdeploy.ru) instead of one scoped to
        # this exact subdomain. Traefik caches ACME certs by domain set, so
        # the first-ever project deploy pays the ~5-15min reg.ru DNS-01
        # propagation wait ONCE — every subsequent project (any slug) reuses
        # the same cached wildcard cert instantly instead of repeating it.
        f"traefik.http.routers.{router}.tls.domains[0].main": f"*.{DOMAIN_SUFFIX}",
        f"traefik.http.routers.{router}.tls.domains[0].sans": DOMAIN_SUFFIX,
        # Explicit service (port) — needed once a second router (custom
        # domain, below) has to reference this same backend by name; Traefik's
        # implicit single-service auto-detection isn't reliably referenceable
        # across multiple routers on one container.
        f"traefik.http.services.{router}.loadbalancer.server.port": str(internal_port),
    }

    if custom_domain:
        # A user's own domain needs its own router + its own certificate —
        # DNS-01 (the "le" resolver above) is only possible for domains we
        # control DNS for (verfdeploy.ru, via reg.ru's API). For a domain a
        # user owns, we have no registrar access, so this router uses
        # HTTP-01 instead (the "http" resolver, configured separately in
        # docker-compose) — it self-verifies ownership implicitly: the
        # challenge can only succeed if the domain's DNS genuinely points
        # here, so there's no separate "verify ownership" step to build.
        custom_router = f"{router}-custom"
        labels.update({
            f"traefik.http.routers.{custom_router}.rule": f"Host(`{custom_domain}`)",
            f"traefik.http.routers.{custom_router}.entrypoints": TRAEFIK_ENTRYPOINT,
            f"traefik.http.routers.{custom_router}.tls.certresolver": "http",
            f"traefik.http.routers.{custom_router}.service": router,
        })

    return labels


def run_container(
    slug: str, image_tag: str, internal_port: int, env: dict | None = None,
    mem_limit: str = DEFAULT_MEM_LIMIT, cpu_quota: int = DEFAULT_CPU_QUOTA,
    custom_domain: str | None = None,
):
    """Start the new container, then stop+remove any previous one for this slug.

    New-then-old ordering keeps the old version answering traffic until the
    new one is confirmed running — a minimal blue/green swap.

    `mem_limit`/`cpu_quota` are resolved by the caller from the project
    owner's plan (app.config.PLAN_MEM_LIMITS / PLAN_CPU_QUOTAS) — this
    function just applies whatever it's given, defaulting to the free tier
    for callers that don't resolve a plan (e.g. admin-created projects).

    `custom_domain`, if set, adds a second Traefik router (HTTP-01 cert)
    routing the user's own domain to this same container alongside the
    normal {slug}.DOMAIN_SUFFIX subdomain.
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
            labels=_labels(slug, internal_port, custom_domain),
            mem_limit=mem_limit,
            cpu_period=100_000,
            cpu_quota=cpu_quota,
            restart_policy={"Name": "unless-stopped"},
            extra_hosts={"api.telegram.org": TELEGRAM_API_PINNED_IP},
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
