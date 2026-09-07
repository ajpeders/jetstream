"""Viewer-page panels ported to Angular islands (queue, requests, library).
Split out of test_admin_island.py so the two sessions working this
tree stop colliding in one file."""
import pathlib

import pytest

_BUNDLE = pathlib.Path(__file__).resolve().parents[1] / "static" / "build-viewer" / "main.js"


def _require_built_bundle():
    """Skip when nothing has built the viewer bundle.

    static/build-viewer/ is a build artifact and gitignored, so it is absent
    from a fresh clone. CI's `tests` job is exactly that: it runs in a
    python:3.12 container, clones the repo itself, and never runs `npm run
    build` — that happens in the separate `checks` job, on a different runner
    with its own filesystem. Asserting on bundle *contents* there would fail
    for the absence of a file rather than for anything about the code.

    Deliberately a skip and not a silent pass: the assertions below are real,
    they just need something to have built first. `npm run build` locally, or
    read them as covered by the checks job proving the build succeeds.
    """
    if not _BUNDLE.exists():
        pytest.skip("viewer bundle not built — run `npm run build:viewer`")


def test_viewer_page_hosts_the_queue_component(viewer_client):
    r = viewer_client.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    assert "jet-queue-panel" in html
    assert "/build-viewer/main.js" in html
    # the section keeps its id so viewer.css needs no changes
    assert 'id="queue-panel"' in html
    # and the hand-written queue rendering is gone
    for gone in ('id="queue-count"', 'id="queue-toggle"', 'id="queue-list"',
                 "queuePoll", "setQueueCollapsed"):
        assert gone not in html, gone


def test_viewer_bundle_is_token_gated(client):
    """No token cookie — the bundle names every endpoint it calls, so it stays
    behind the same gate as /build/."""
    assert client.get("/build-viewer/main.js").status_code in (401, 403)


def test_viewer_bundle_serves_to_a_token_holder(viewer_client):
    _require_built_bundle()
    r = viewer_client.get("/build-viewer/main.js")
    assert r.status_code == 200
    # module scripts are rejected outright on a non-JS MIME type
    assert "javascript" in r.headers["Content-Type"]


def _seed_request(client, app_module, name="seed.mkv"):
    """File a request against a real media file and return its id. The test
    library starts empty, so the file has to be made first — under the viewer
    root from conftest, since only paths beneath it are requestable."""
    (app_module.MEDIA_ROOT / "films" / name).write_bytes(b"not really a video")
    r = client.post("/api/request", json={"path": f"films/{name}", "sid": "testsid"})
    assert r.status_code == 200, (r.status_code, r.data)
    items = client.get("/api/requests?sid=testsid").get_json()["requests"]
    assert items, "request was filed but does not appear in the feed"
    return items[0]["id"]


def test_requests_feed_is_shaped_as_the_component_expects(viewer_client, app_module):
    """The component types against both halves of this payload: `requests`
    drives the list, `can_manage` gates the Queue/deny buttons."""
    _seed_request(viewer_client, app_module, "shape.mkv")
    j = viewer_client.get("/api/requests?sid=testsid").get_json()
    assert isinstance(j["can_manage"], bool)
    it = j["requests"][0]
    # every field the component's MediaRequest interface names
    for field in ("id", "title", "requester", "votes", "voted"):
        assert field in it, field
    assert isinstance(it["votes"], int) and isinstance(it["voted"], bool)


def test_vote_toggles_and_reports_the_new_tally(viewer_client, app_module):
    """The component re-polls on a truthy response rather than trusting the
    body, but the toggle has to actually toggle for the arrow to mean anything."""
    rid = _seed_request(viewer_client, app_module, "vote.mkv")
    first = viewer_client.post(f"/api/request/{rid}/vote", json={"sid": "testsid"})
    assert first.status_code == 200
    second = viewer_client.post(f"/api/request/{rid}/vote", json={"sid": "testsid"})
    assert second.status_code == 200
    assert first.get_json()["voted"] is not second.get_json()["voted"]


def test_deny_removes_the_request(viewer_client, app_module):
    """The panel's ✕ button. viewer_client holds an admin token, so it clears
    the control-tier gate the same way a friend's browser does."""
    rid = _seed_request(viewer_client, app_module, "deny.mkv")
    assert viewer_client.delete(f"/api/control/requests/{rid}").status_code == 200
    remaining = viewer_client.get("/api/requests?sid=testsid").get_json()["requests"]
    assert all(x["id"] != rid for x in remaining)


def test_viewer_page_hosts_the_requests_component(viewer_client):
    html = viewer_client.get("/").data.decode()
    assert "jet-requests-panel" in html
    assert 'id="reqlist-panel"' in html  # viewer.css + the theme key off this
    for gone in ('id="reqlist-count"', 'id="reqlist-toggle"', "reqListPoll",
                 "voteRequest", "approveRequest", "denyRequest"):
        assert gone not in html, gone


def test_the_refresh_bridge_no_longer_crosses_the_page_boundary(viewer_client):
    """The bridge used to span inline script → bundle. Now that the library
    panel is ported, both the callers and the publishers are inside the bundle,
    so the page itself should hold no window.jetstream call at all.

    The hooks have NOT gone away, and won't: each panel is its own
    bootstrapApplication, so islands have separate root injectors and cannot
    reach each other through DI. window.jetstream is how the library panel
    nudges the request list and how that list nudges the queue."""
    html = viewer_client.get("/").data.decode()
    assert "window.jetstream" not in html
    assert "jetstreamRefreshRequests" not in html  # the pre-bridge name

    _require_built_bundle()
    js = viewer_client.get("/build-viewer/main.js").data.decode()
    for name in ("requests", "queue"):
        assert f'"{name}"' in js or f"'{name}'" in js, name


def test_viewer_page_hosts_the_library_component(viewer_client):
    html = viewer_client.get("/").data.decode()
    assert "jet-library-panel" in html
    assert 'id="request-panel"' in html  # viewer.css + the theme key off this
    # the panel is expanded by default, so it must not ship the collapsed class
    assert 'id="request-panel" jet-library-panel' in html
    # and the ~260 lines of hand-written browse/search/file are gone
    for gone in ('id="request-search"', 'id="library-back"', 'id="request-toggle"',
                 "browseLibrary", "requestDoSearch", "sendRequest", "renderFileResult",
                 "friendlyDirectoryName", "libraryLabel"):
        assert gone not in html, gone


def test_library_browse_is_shaped_as_the_component_expects(viewer_client, app_module):
    """A bare array (not an envelope), whose entries carry the three fields the
    component's LibraryItem interface reads."""
    (app_module.MEDIA_ROOT / "films" / "browse.mkv").write_bytes(b"x")

    roots = viewer_client.get("/api/library/browse").get_json()
    assert isinstance(roots, list), roots
    assert roots and roots[0]["type"] == "directory"
    # the configured root carries the flag friendlyDirectoryName() prefers
    assert roots[0].get("viewer_root") is True

    inside = viewer_client.get("/api/library/browse?path=films").get_json()
    entry = next(e for e in inside if e["type"] == "file")
    for field in ("name", "path", "type"):
        assert field in entry, field


def test_library_search_reports_truncation(viewer_client, app_module):
    """The component surfaces `truncated`; the hand-written panel dropped it,
    which made a capped list look like the whole library."""
    (app_module.MEDIA_ROOT / "films" / "findme.mkv").write_bytes(b"x")
    j = viewer_client.get("/api/library/search?q=findme").get_json()
    assert isinstance(j["truncated"], bool)
    assert any(r["name"] == "findme.mkv" for r in j["results"])


def test_filing_a_request_answers_what_the_button_renders(viewer_client, app_module):
    """The Request button distinguishes success from a rate-limit by reading
    `error` off the body, so the failure path has to answer JSON, not just 4xx."""
    (app_module.MEDIA_ROOT / "films" / "file-me.mkv").write_bytes(b"x")
    ok = viewer_client.post("/api/request",
                            json={"path": "films/file-me.mkv", "name": "", "sid": "testsid"})
    assert ok.status_code == 200

    # a path outside the viewer roots is the refusal the button shows "failed" for
    bad = viewer_client.post("/api/request",
                             json={"path": "../etc/passwd", "name": "", "sid": "testsid"})
    assert bad.status_code == 400
    assert "error" in bad.get_json()
