import pytest

from app.bot import build_ombor_client
from app.config import load_config

TOKEN = "t" * 40


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    monkeypatch.setenv("SALES_BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("SALES_GROUP_CHAT_ID", "-100123")
    monkeypatch.setenv("ANALYTICS_DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("OMBOR_API_BASE_URL", "http://ombor.test")


def _headers(config):
    return build_ombor_client(config)._client._headers()


def test_token_from_env_is_sent_as_bearer(monkeypatch):
    monkeypatch.setenv("OMBOR_API_TOKEN", f"  {TOKEN}\n")
    config = load_config()
    assert config.ombor_api_token == TOKEN
    headers = _headers(config)
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-User-Name"] == "sales-sync"  # orqaga qaytish uchun eski header ham bor


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_token_keeps_previous_behavior(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("OMBOR_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OMBOR_API_TOKEN", value)
    config = load_config()
    assert config.ombor_api_token is None
    assert "Authorization" not in _headers(config)
