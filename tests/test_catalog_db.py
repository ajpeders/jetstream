import sqlite3


def _catalog_viewer_client(app_module, tmp_path):
    app_module.CATALOG_DB = tmp_path / "catalog.sqlite3"
    app_module.VIEWER_LIBRARY_ROOTS_RAW = "Films=films"
    (app_module.MEDIA_ROOT / "films").mkdir(exist_ok=True)
    token = "catalog-test-token"
    with app_module.tokens_lock:
        if not any(t.get("id") == token for t in app_module.tokens):
            app_module.tokens.append(
                {"id": token, "label": "catalog", "level": "admin", "created": 0}
            )
    c = app_module.app.test_client()
    c.set_cookie(app_module.TOKEN_COOKIE, token, domain="localhost")
    return c


def _reset_catalog(app_module):
    if app_module.CATALOG_DB.exists():
        app_module.CATALOG_DB.unlink()
    with app_module._library_cache_lock:
        app_module._library_cache["sig"] = None
        app_module._library_cache["files"] = None


def test_catalog_db_backs_library_browse(app_module, tmp_path):
    viewer_client = _catalog_viewer_client(app_module, tmp_path)
    _reset_catalog(app_module)
    (app_module.MEDIA_ROOT / "films" / "Catalog Movie.mkv").write_bytes(b"movie")

    rows = viewer_client.get("/api/library/browse?path=films").get_json()

    assert app_module.CATALOG_DB.exists()
    item = next(row for row in rows if row["name"] == "Catalog Movie.mkv")
    assert item["path"] == "films/Catalog Movie.mkv"
    assert item["type"] == "file"
    assert item["size"] == 5

    with sqlite3.connect(app_module.CATALOG_DB) as conn:
        count = conn.execute("SELECT count(*) FROM catalog_items WHERE type = 'file'").fetchone()[0]
    assert count >= 1


def test_catalog_search_respects_viewer_roots(app_module, tmp_path):
    viewer_client = _catalog_viewer_client(app_module, tmp_path)
    _reset_catalog(app_module)
    (app_module.MEDIA_ROOT / "films" / "visible-find.mkv").write_bytes(b"x")
    (app_module.MEDIA_ROOT / "private").mkdir(exist_ok=True)
    (app_module.MEDIA_ROOT / "private" / "hidden-find.mkv").write_bytes(b"x")

    j = viewer_client.get("/api/library/search?q=find").get_json()
    names = {row["name"] for row in j["results"]}

    assert "visible-find.mkv" in names
    assert "hidden-find.mkv" not in names
    assert j["truncated"] is False


def test_catalog_refreshes_when_media_tree_changes(app_module, tmp_path):
    viewer_client = _catalog_viewer_client(app_module, tmp_path)
    _reset_catalog(app_module)
    (app_module.MEDIA_ROOT / "films" / "first-refresh.mkv").write_bytes(b"1")
    assert viewer_client.get("/api/library/search?q=refresh").get_json()["results"]

    (app_module.MEDIA_ROOT / "films" / "second-refresh.mkv").write_bytes(b"2")
    names = {
        row["name"]
        for row in viewer_client.get("/api/library/search?q=second-refresh").get_json()["results"]
    }

    assert "second-refresh.mkv" in names
