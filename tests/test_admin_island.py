"""Admin panels ported from hand-written JS to Angular islands.
Owned by this session under the two-session file split; viewer-side
assertions live in test_viewer_island.py."""


def test_admin_renders_component_host(client):
    r = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"})
    assert r.status_code == 200, r.status_code
    html = r.data.decode()
    assert "<jet-viewers-panel></jet-viewers-panel>" in html
    assert "/build-admin/main.js" in html
    # the hand-written panel is gone
    for gone in ('id="viewers-list"', 'id="viewers-count"', 'id="viewers-history"',
                 "refreshViewers", "viewersHistoryBtn"):
        assert gone not in html, gone
    # but the shared helper the other panels still need survived the port
    assert "function fmtAgo(secs)" in html


def test_viewers_api_still_shaped_as_the_component_expects(client):
    h = {"X-Admin-Proxy": "test-proxy-secret"}
    r = client.get("/admin/api/viewers", headers=h)
    assert r.status_code == 200
    assert isinstance(r.get_json(), list)
    hist = client.get("/admin/api/viewers/history", headers=h)
    assert hist.status_code == 200
    assert isinstance(hist.get_json(), list)



def test_admin_bundle_is_token_gated(client):
    """The Angular admin bundle must stay behind auth like /build/ does —
    it names every /admin/api/* route it calls."""
    r = client.get("/build-admin/main.js")
    assert r.status_code in (401, 403), r.status_code


def test_admin_hosts_the_reports_component(client):
    r = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"})
    assert r.status_code == 200
    html = r.data.decode()
    assert "<jet-reports-panel></jet-reports-panel>" in html
    assert 'id="reports-panel"' in html  # css/admin.css keys off this
    for gone in ('id="reports-list"', 'id="reports-count"', "refreshReports",
                 "reportMetaLine", "renderTriage"):
        assert gone not in html, gone
    # reportAgo outlived this port for the media-requests panel, then died with
    # it — test_admin_hosts_both_request_panels now asserts it is gone.


def test_report_mutations_answer_the_shapes_the_component_sends(viewer_client):
    """The component's three writes, with the exact payloads its service uses.
    Filing a report needs a viewer token; the admin reads need the proxy header."""
    client = viewer_client
    h = {"X-Admin-Proxy": "test-proxy-secret"}
    assert client.post("/api/report", json={"message": "test"}).status_code == 200
    rid = client.get("/admin/api/reports", headers=h).get_json()[0]["id"]

    r = client.post(f"/admin/api/reports/{rid}/handled", json={"unhandle": False}, headers=h)
    assert r.status_code == 200 and r.get_json()["status"] == "handled"

    r = client.post(f"/admin/api/reports/{rid}/handled", json={"unhandle": True}, headers=h)
    assert r.status_code == 200 and r.get_json()["status"] == "new"

    assert client.delete(f"/admin/api/reports/{rid}", headers=h).status_code == 200

    # clear-all reports its count, which the success toast interpolates
    client.post("/api/report", json={"message": "another"})
    r = client.delete("/admin/api/reports", headers=h)
    assert r.status_code == 200 and "cleared" in r.get_json()


def test_admin_hosts_the_two_small_panels(client):
    html = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"}).data.decode()
    assert "<jet-custom-reactions-panel>" in html
    assert "<jet-vod-sessions-panel>" in html
    # containers keep their ids so css/admin.css still applies
    assert 'id="reactions-panel"' in html and 'id="vod-panel"' in html
    for gone in ("refreshCustomReactions", "refreshVodSessions",
                 'id="reactions-grid"', 'id="vod-sessions"', 'id="vod-count"'):
        assert gone not in html, gone


def test_admin_bridge_no_longer_crosses_the_page_boundary(client):
    """Both admin hooks used to be page->bundle. Porting the users panel put
    the callers inside the bundle, so the page holds none. They still route
    through window because each panel is its own bootstrapApplication, and so
    its own root injector — finishing the port cannot retire them, only
    merging the islands under one bootstrap can."""
    html = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"}).data.decode()
    for gone in ("window.jetstream?.vodSessions", "window.jetstream?.users",
                 "loadUsers"):
        assert gone not in html, gone


def test_new_panel_endpoints_answer(client):
    h = {"X-Admin-Proxy": "test-proxy-secret"}
    assert isinstance(client.get("/admin/api/reactions", headers=h).get_json(), list)
    feed = client.get("/admin/api/vod/sessions", headers=h).get_json()
    assert "sessions" in feed and "cap" in feed


def test_admin_hosts_both_request_panels(client):
    html = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"}).data.decode()
    assert "<jet-queue-requests-panel>" in html
    assert "<jet-media-requests-panel>" in html
    assert 'id="requests-panel"' in html and 'id="media-requests-panel"' in html
    for gone in ("refreshRequests", "refreshMediaRequests", "mediaRequestMeta",
                 'id="requests-list"', 'id="media-requests-list"',
                 # dead once their only callers moved to Angular
                 "function reportAgo", "function basename"):
        assert gone not in html, gone
    # still used by the un-ported panels, so it must survive
    assert "function fmtAgo(secs)" in html


def test_queue_request_approve_reaches_the_queue(viewer_client, app_module):
    """The panel's Add button, then the window.loadQueue hop it triggers."""
    h = {"X-Admin-Proxy": "test-proxy-secret"}
    (app_module.MEDIA_ROOT / "films" / "add.mkv").write_bytes(b"x")
    assert viewer_client.post("/api/request", json={"path": "films/add.mkv", "sid": "s"}).status_code == 200
    rid = viewer_client.get("/admin/api/requests", headers=h).get_json()[0]["id"]
    assert viewer_client.post(f"/admin/api/requests/{rid}/approve", headers=h).status_code == 200
    titles = [i["title"] for i in viewer_client.get("/api/queue").get_json()["queue"]]
    assert any("add" in (t or "") for t in titles), titles


def test_media_requests_endpoint_answers(client):
    r = client.get("/admin/api/media/requests", headers={"X-Admin-Proxy": "test-proxy-secret"})
    assert r.status_code == 200
    assert isinstance(r.get_json(), list)


def test_admin_hosts_the_users_panel(client):
    html = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"}).data.decode()
    assert "<jet-users-panel></jet-users-panel>" in html
    assert 'id="users-panel"' in html  # css/admin.css keys off this
    for gone in ("loadUsers", "renderUsers", "userWhen",
                 'id="users-list"', 'id="user-form"', 'id="user-form-err"'):
        assert gone not in html, gone


def test_users_panel_bootstraps_itself(client):
    """Regression: porting the VOD panel deleted the `if (IS_ADMIN)` block that
    also made the initial loadUsers() call and set its interval, so the users
    list rendered 'Loading…' forever. The component polls on its own now, and
    the page must not be relied on to prime it."""
    html = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"}).data.decode()
    # scoped to this panel's own placeholder — the library browser legitimately
    # still ships a "Loading…" row of its own
    assert '<ul id="users-list">' not in html
    assert "loadUsers()" not in html


def test_user_lifecycle_matches_what_the_panel_sends(client):
    """Create, duplicate-rejection, disable, password, delete — the five writes
    the panel makes, with its payloads."""
    h = {"X-Admin-Proxy": "test-proxy-secret"}
    made = client.post("/admin/api/users", json={"username": "pytestuser",
                                                 "password": "hunter2hunter2"}, headers=h)
    assert made.status_code == 200, made.data
    uid = made.get_json()["id"]

    dup = client.post("/admin/api/users", json={"username": "pytestuser",
                                                "password": "hunter2hunter2"}, headers=h)
    # the component keys its inline "already taken" message off this exact body
    assert dup.status_code == 409 and dup.get_json()["error"] == "duplicate"

    assert client.post(f"/admin/api/users/{uid}/disabled", json={"disabled": True},
                       headers=h).status_code == 200
    assert client.post(f"/admin/api/users/{uid}/password", json={"password": "newpassword123"},
                       headers=h).status_code == 200
    assert client.delete(f"/admin/api/users/{uid}", headers=h).status_code == 200


def test_bundle_to_page_hop_still_has_its_target(client):
    """queue-requests-panel calls window.loadQueue?.() after approving. That
    function must still exist on the page until the queue panel is ported."""
    html = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"}).data.decode()
    assert "async function loadQueue()" in html


def test_admin_hosts_the_chat_panel(client):
    html = client.get("/admin", headers={"X-Admin-Proxy": "test-proxy-secret"}).data.decode()
    assert "jet-chat-panel" in html
    assert 'id="chat-panel"' in html  # css/admin.css keys off this
    for gone in ("chatPoll", "chatRender", "chatApplyDeletions", "chatColor",
                 'id="chat-messages"', 'id="chat-form"', 'id="chat-toggle"'):
        assert gone not in html, gone


def test_chat_delta_contract(viewer_client):
    """The panel appends rather than rebuilding, so it depends on ?since=
    returning only what is new and reporting max_id."""
    for text in ("one", "two"):
        assert viewer_client.post("/chat/send",
                                  json={"message": text, "sid": "t1"}).status_code == 200
    first = viewer_client.get("/chat/recent?since=0").get_json()
    assert len(first["messages"]) == 2
    caught_up = viewer_client.get(f"/chat/recent?since={first['max_id']}").get_json()
    assert caught_up["messages"] == []
    assert caught_up["max_id"] == first["max_id"]


def test_chat_moderation_endpoints(viewer_client):
    """Delete and mute, with the payloads the panel's buttons send."""
    h = {"X-Admin-Proxy": "test-proxy-secret"}
    viewer_client.post("/chat/send", json={"message": "moderate me", "sid": "t2"})
    msgs = viewer_client.get("/chat/recent?since=0").get_json()["messages"]
    mid = msgs[-1]["id"]
    assert viewer_client.delete(f"/admin/api/chat/{mid}", headers=h).status_code == 200

    muted = viewer_client.post("/admin/api/chat/mute",
                               json={"sid": "t2", "seconds": 300}, headers=h)
    assert muted.status_code == 200 and muted.get_json()["muted"] is True
    un = viewer_client.post("/admin/api/chat/mute",
                            json={"sid": "t2", "seconds": 0}, headers=h)
    assert un.status_code == 200 and un.get_json()["muted"] is False
