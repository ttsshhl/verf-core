from app.deployer import _labels
from app.config import DOMAIN_SUFFIX


def test_labels_request_wildcard_cert_not_per_subdomain_cert():
    """The whole point of this: every project's router asks Traefik for the
    *wildcard* cert (*.verfdeploy.ru), not a cert scoped to its own exact
    subdomain — so only the very first deploy ever pays the DNS-01
    propagation wait, and every project after that reuses the cached cert.
    """
    labels = _labels("my-project", 8000)
    assert labels["traefik.http.routers.verf-my-project.tls.domains[0].main"] == f"*.{DOMAIN_SUFFIX}"
    assert labels["traefik.http.routers.verf-my-project.tls.domains[0].sans"] == DOMAIN_SUFFIX


def test_labels_host_rule_still_scoped_to_exact_subdomain():
    """Routing itself must stay per-project — only the TLS cert is shared."""
    labels = _labels("my-project", 8000)
    assert labels["traefik.http.routers.verf-my-project.rule"] == f"Host(`my-project.{DOMAIN_SUFFIX}`)"


def test_labels_different_slugs_produce_different_routers_same_wildcard_domain():
    labels_a = _labels("proj-a", 8000)
    labels_b = _labels("proj-b", 3000)
    assert "traefik.http.routers.verf-proj-a.rule" in labels_a
    assert "traefik.http.routers.verf-proj-b.rule" in labels_b
    assert (
        labels_a["traefik.http.routers.verf-proj-a.tls.domains[0].main"]
        == labels_b["traefik.http.routers.verf-proj-b.tls.domains[0].main"]
    )


def test_labels_explicit_service_port_matches_internal_port():
    labels = _labels("my-project", 3000)
    assert labels["traefik.http.services.verf-my-project.loadbalancer.server.port"] == "3000"


def test_labels_without_custom_domain_has_single_router():
    labels = _labels("solo-project", 8000)
    router_keys = [k for k in labels if k.startswith("traefik.http.routers.")]
    router_names = {k.split(".")[3] for k in router_keys}
    assert router_names == {"verf-solo-project"}


def test_labels_with_custom_domain_adds_second_router_with_http_challenge():
    labels = _labels("my-project", 8000, custom_domain="example.com")
    custom_router = "verf-my-project-custom"
    assert labels[f"traefik.http.routers.{custom_router}.rule"] == "Host(`example.com`)"
    assert labels[f"traefik.http.routers.{custom_router}.tls.certresolver"] == "http"
    assert labels[f"traefik.http.routers.{custom_router}.service"] == "verf-my-project"


def test_labels_custom_domain_does_not_affect_base_router_certresolver():
    """The subdomain router must keep using DNS-01/wildcard (le) — only the
    custom-domain router switches to HTTP-01 (http)."""
    labels = _labels("my-project", 8000, custom_domain="example.com")
    assert labels["traefik.http.routers.verf-my-project.tls.certresolver"] == "le"

