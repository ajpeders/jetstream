"""Smoke tests: the app imports, and the three load-bearing auth behaviors
hold. Deliberately narrow — the value is that CI now executes the auth path
at all, not that it covers it."""


def test_login_page_serves(client):
    assert client.get("/login").status_code == 200


def test_admin_rejected_without_proxy_header(client):
    # ADMIN_PROXY_SECRET is set (conftest), so a request without the matching
    # X-Admin-Proxy header must be refused — this is the web-network bypass
    # gate (2026-08-03 incident).
    r = client.post("/admin/api/users", json={"username": "x", "password": "y"})
    assert r.status_code == 403


def test_admin_passes_gate_with_proxy_header(client):
    # With the right header the proxy gate must NOT be the thing that blocks;
    # anything but 403-forbidden from the gate is fine here (the endpoint
    # itself may still reject the payload).
    r = client.post(
        "/admin/api/users",
        json={},
        headers={"X-Admin-Proxy": "test-proxy-secret"},
    )
    assert r.status_code != 403 or b"forbidden" not in r.data


def test_viewer_route_gated_without_token(client):
    r = client.get("/api/status")
    assert r.status_code in (401, 302), r.status_code


def test_now_playing_is_public(client):
    assert client.get("/api/now-playing").status_code == 200
