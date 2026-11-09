"""Startup guard against the development default secrets on Cloud Run."""
from types import SimpleNamespace

import pytest

from app.config import DEFAULT_SECRET, validate_production_secrets

STRONG = "a" * 64


def config(secret_key=STRONG, token=STRONG):
    return SimpleNamespace(SECRET_KEY=secret_key, SECURITY_TOKEN=token)


def test_defaults_are_allowed_outside_cloud_run():
    assert validate_production_secrets(config(DEFAULT_SECRET, DEFAULT_SECRET), {}) == []


@pytest.mark.parametrize("environ", [{"K_SERVICE": "plexus"}, {"CLOUD_RUN_JOB": "pipeline"}])
def test_default_secret_stops_startup_on_cloud_run(environ):
    with pytest.raises(RuntimeError, match="SECURITY_TOKEN"):
        validate_production_secrets(config(token=DEFAULT_SECRET), environ)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_production_secrets(config(secret_key=DEFAULT_SECRET), environ)


def test_both_defaults_are_reported_together():
    with pytest.raises(RuntimeError, match="SECRET_KEY e SECURITY_TOKEN"):
        validate_production_secrets(config(DEFAULT_SECRET, DEFAULT_SECRET), {"K_SERVICE": "x"})


def test_strong_secrets_start_cleanly_on_cloud_run():
    assert validate_production_secrets(config(), {"K_SERVICE": "x"}) == []


def test_short_custom_secret_only_warns_and_never_blocks():
    warnings = validate_production_secrets(config(token="short-but-custom"), {"K_SERVICE": "x"})
    assert len(warnings) == 1 and "SECURITY_TOKEN" in warnings[0]
    assert validate_production_secrets(config(secret_key="tiny"), {}) != []


def test_a_production_domain_alone_does_not_trigger_the_guard():
    # Local runs often carry the production DOMAIN in .env for the deploy scripts.
    assert validate_production_secrets(config(DEFAULT_SECRET, DEFAULT_SECRET), {"DOMAIN": "https://prod.example"}) == []
