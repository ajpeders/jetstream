"""Authenticated hub page ported from inline script to a React island."""
import pathlib

import pytest

_BUNDLE = pathlib.Path(__file__).resolve().parents[1] / "static" / "build" / "hub.js"
_ADMIN = {"X-Admin-Proxy": "test-proxy-secret"}


def _require_built_bundle():
    if not _BUNDLE.exists():
        pytest.skip("hub bundle not built — run `npm run build`")


def _login(client, username="hubtest", password="hubpassword"):
    made = client.post(
        "/admin/api/users",
        json={"username": username, "password": password},
        headers=_ADMIN,
    )
    assert made.status_code in (200, 409), made.data
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.data


def test_hub_redirects_when_logged_out(client):
    r = client.get("/home")
    assert r.status_code in (301, 302)
    assert "/login?next=/home" in r.headers["Location"]


def test_hub_hosts_the_component(client):
    _login(client)
    r = client.get("/home")
    assert r.status_code == 200
    html = r.data.decode()
    assert "jet-hub-header" in html
    assert "jet-hub-main" in html
    assert "/build/hub.js" in html
    assert 'class="hub-page"' in html
    for gone in ("pwOpen", "pwSetError", "addEventListener", 'id="pw-current"'):
        assert gone not in html, gone


def test_hub_bundle_is_token_gated(client):
    assert client.get("/build/hub.js").status_code in (401, 403)


def test_hub_bundle_serves_to_a_logged_in_user(client):
    _require_built_bundle()
    _login(client, "hubbundle", "hubpassword")
    r = client.get("/build/hub.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["Content-Type"]


def test_password_change_contract(client):
    _login(client, "hubpw", "oldpassword")
    bad = client.post(
        "/api/auth/password",
        json={"current_password": "wrong", "new_password": "newpassword"},
    )
    assert bad.status_code == 401 and bad.get_json()["error"] == "bad_password"

    short = client.post(
        "/api/auth/password",
        json={"current_password": "oldpassword", "new_password": "short"},
    )
    assert short.status_code == 400 and short.get_json()["error"] == "bad_new_password"

    ok = client.post(
        "/api/auth/password",
        json={"current_password": "oldpassword", "new_password": "newpassword"},
    )
    assert ok.status_code == 200
