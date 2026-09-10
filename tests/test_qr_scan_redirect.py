"""The QR scan redirect must always name an absolute frontend host.

A base URL that is empty, blank or scheme-less makes FastAPI emit a relative
``Location`` header, so the customer's browser resolves it against the API host
and lands on a page the API does not serve. Every scan for a client without
``redirect_base_url`` broke this way while ``FRONTEND_BASE_URL`` was unset.
"""

import importlib

import pytest


@pytest.fixture
def scan_qr(monkeypatch):
    monkeypatch.delenv("FRONTEND_BASE_URL", raising=False)
    import routers.qr.scan_qr as module

    return importlib.reload(module)


@pytest.mark.parametrize(
    "configured",
    [None, "", "   ", "www.activlink.io", "activlink.io/product"],
)
def test_unusable_client_base_url_falls_back_to_an_absolute_host(scan_qr, configured):
    """A missing or scheme-less ``redirect_base_url`` must not go relative."""
    resolved = scan_qr._resolve_base_url(configured)

    assert resolved.startswith("https://")
    assert resolved == scan_qr.DEFAULT_FRONTEND_BASE_URL


def test_client_base_url_wins_when_absolute(scan_qr):
    assert (
        scan_qr._resolve_base_url("https://client.example.com/")
        == "https://client.example.com"
    )
    assert (
        scan_qr._resolve_base_url("http://staging.example.com")
        == "http://staging.example.com"
    )


def test_env_var_overrides_the_default_for_clients_without_an_override(monkeypatch):
    monkeypatch.setenv("FRONTEND_BASE_URL", "https://env.example.com/")
    import routers.qr.scan_qr as module

    module = importlib.reload(module)

    assert module._resolve_base_url(None) == "https://env.example.com"
    assert module._resolve_base_url("") == "https://env.example.com"
