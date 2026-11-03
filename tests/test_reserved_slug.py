import pytest
from pydantic import ValidationError

from app.schemas import ProjectCreate


@pytest.mark.parametrize("slug", ["api", "cabinet", "www", "core-traefik-1", "core-verf-core-1"])
def test_reserved_slugs_rejected(slug):
    with pytest.raises(ValidationError):
        ProjectCreate(slug=slug)


def test_normal_slug_ok():
    assert ProjectCreate(slug="my-bot").slug == "my-bot"
