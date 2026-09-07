"""Ad-hoc: the extracted stylesheets must be reachable exactly where their
pages are — logged-out for the two front-door pages, token-gated otherwise."""

def test_logged_out_css_is_served(client):
    for path in ("/css/login.css", "/css/home.css"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert b"box-sizing" in r.data

def test_gated_css_is_not_public(client):
    for path in ("/css/viewer.css", "/css/admin.css", "/css/library.css", "/css/hub.css"):
        r = client.get(path)
        assert r.status_code in (401, 403), (path, r.status_code)

def test_login_page_links_its_css(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b'href="/css/login.css"' in r.data
