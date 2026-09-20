"""/api/_authcheck: LAN clients pass without a cookie AND are counted as
viewers (so LIVE_IDLE_TIMEOUT_S doesn't kill the transcode under the TV)."""


def _hdrs(ip):
    return {"X-Real-IP": ip, "X-Forwarded-For": ip, "User-Agent": "libmpv"}


def test_remote_without_cookie_is_401(app_module, client):
    with app_module.settings_lock:
        app_module.settings["viewer_public"] = False
    r = client.get("/api/_authcheck", headers=_hdrs("203.0.113.9"))
    assert r.status_code == 401


def test_lan_without_cookie_is_allowed_and_tracked(app_module, client):
    with app_module.settings_lock:
        app_module.settings["viewer_public"] = False
    r = client.get("/api/_authcheck", headers=_hdrs("192.168.0.220"))
    assert r.status_code == 204
    with app_module.viewers_lock:
        assert "192.168.0.220" in app_module.viewers
    assert app_module._viewer_count() >= 1


def test_traefik_subnet_is_not_lan(app_module):
    assert not app_module._is_lan_client("172.24.0.5")
    assert not app_module._is_lan_client("garbage")
    assert app_module._is_lan_client("10.8.0.15")
