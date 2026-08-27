from app.deployer import _labels
from app.config import DOMAIN_SUFFIX


def test_labels_request_wildcard_cert_not_per_subdomain_cert():
    """The whole point of this: every project's router asks Traefik for the
    *wildcard* cert (*.verfdeploy.ru), not a cert scoped to its own exact
    subdomain — so only the very first deploy ever pays the DNS-01
    propagation wait, and every project after that reuses the cached cert.
    """
    labels = _labels("my-project")
    assert labels["traefik.http.routers.verf-my-project.tls.domains[0].main"] == f"*.{DOMAIN_SUFFIX}"
    assert labels["traefik.http.routers.verf-my-project.tls.domains[0].sans"] == DOMAIN_SUFFIX


def test_labels_host_rule_still_scoped_to_exact_subdomain():
    """Routing itself must stay per-project — only the TLS cert is shared."""
    labels = _labels("my-project")
    assert labels["traefik.http.routers.verf-my-project.rule"] == f"Host(`my-project.{DOMAIN_SUFFIX}`)"


def test_labels_different_slugs_produce_different_routers_same_wildcard_domain():
    labels_a = _labels("proj-a")
    labels_b = _labels("proj-b")
    assert "traefik.http.routers.verf-proj-a.rule" in labels_a
    assert "traefik.http.routers.verf-proj-b.rule" in labels_b
    assert (
        labels_a["traefik.http.routers.verf-proj-a.tls.domains[0].main"]
        == labels_b["traefik.http.routers.verf-proj-b.tls.domains[0].main"]
    )
