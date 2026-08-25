import os

from scripts.seed_allowed_emails import normalize_adc_environment


def test_empty_credentials_path_is_removed_for_adc(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    normalize_adc_environment()
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_nonempty_credentials_path_is_preserved(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/local-key.json")
    normalize_adc_environment()
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/tmp/local-key.json"
