"""Point every state path at a throwaway tmpdir BEFORE app.py is imported —
importing the module starts its worker threads, so the env must be in place
first. This is why app is imported lazily inside the fixture, not at top."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="jetstream-test-"))
_ENV = {
    "HLS_DIR": str(_TMP / "hls"),
    "MEDIA_ROOT": str(_TMP / "media"),
    "REACTIONS_DIR": str(_TMP / "reactions"),
    **{k: str(_TMP / f"{k.lower()}.json") for k in (
        "TOKENS_FILE", "USERS_FILE", "USER_SESSIONS_FILE", "PROGRESS_FILE",
        "PLAYLIST_FILE", "RECENT_FILE", "REQUESTS_FILE", "MEDIA_REQUESTS_FILE",
        "REPORTS_FILE", "CUSTOM_REACTIONS_FILE", "SETTINGS_FILE", "STATE_FILE",
    )},
    "CATALOG_DB": str(_TMP / "catalog.sqlite3"),
    "VIEWER_LOG_FILE": str(_TMP / "viewer_log.jsonl"),
    "ADMIN_PROXY_SECRET": "test-proxy-secret",
    # Without a viewer-facing root nothing under MEDIA_ROOT is requestable —
    # _viewer_path_allowed rejects every path — so the request endpoints can
    # only ever answer "file not found". One root makes them exercisable.
    "VIEWER_LIBRARY_ROOTS": "Films=films",
}
os.environ.update(_ENV)
(_TMP / "media").mkdir()
# Must exist before app.py is imported: _viewer_library_roots only exposes
# roots that resolve to a real directory under MEDIA_ROOT.
(_TMP / "media" / "films").mkdir()


@pytest.fixture(scope="session")
def app_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app  # noqa: E402 — env above must win before this import
    return app


@pytest.fixture()
def client(app_module):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture()
def viewer_client(app_module):
    """A client that has passed the token gate — what a real viewer's browser
    looks like once it has followed an invite link."""
    app_module.app.config["TESTING"] = True
    token = "testviewertoken0"
    with app_module.tokens_lock:
        if not any(t.get("id") == token for t in app_module.tokens):
            app_module.tokens.append(
                {"id": token, "label": "test", "level": "admin", "created": 0})
    c = app_module.app.test_client()
    c.set_cookie(app_module.TOKEN_COOKIE, token, domain="localhost")
    return c
