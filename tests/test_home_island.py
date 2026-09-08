"""The logged-out front door (home.html) ported to an Angular island.

Unlike the viewer and admin bundles, /build-public/ is served to anonymous
callers by design: the page it drives is the 401 body, so a gated bundle
would leave the front door with no working form. It carries only what the
inline script it replaced already showed the world (redeem + register)."""
import pathlib

import pytest

_BUNDLE = pathlib.Path(__file__).resolve().parents[1] / "static" / "build-public" / "main.js"


def _require_built_bundle():
    """Same rationale as test_viewer_island: the bundle is a gitignored build
    artifact, absent in CI's tests job — skip rather than fail on its absence."""
    if not _BUNDLE.exists():
        pytest.skip("public bundle not built — run `npm run build:public`")


def test_front_door_hosts_the_component(client):
    """The home screen is what an anonymous visitor gets at / (with a 401)."""
    r = client.get("/")
    assert r.status_code == 401
    html = r.data.decode()
    assert "jet-front-door" in html
    assert "/build-public/main.js" in html
    # the shell keeps the card so home.css and the theme still match
    assert 'class="card du-card"' in html
    # and the hand-written state machine is gone
    for gone in ('id="state-code"', 'id="register-form"', "addEventListener",
                 "acceptedCode", "USERNAME_RE"):
        assert gone not in html, gone


def test_public_bundle_is_not_gated(client):
    """No cookie, no token: the gate must not answer 401/403 for the bundle.
    Without a build on disk Flask's static route 404s, which is fine here —
    the point is that the *gate* let the request through."""
    assert client.get("/build-public/main.js").status_code not in (401, 403)


def test_public_bundle_serves_anonymously(client):
    _require_built_bundle()
    r = client.get("/build-public/main.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["Content-Type"]


def test_viewer_and_admin_bundles_stay_gated(client):
    """Opening /build-public/ must not loosen the neighbours."""
    for path in ("/build/admin.js", "/build/viewer.js"):
        assert client.get(path).status_code in (401, 403), path
