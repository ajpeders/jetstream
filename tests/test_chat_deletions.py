"""api_chat_recent's deleted-id bookkeeping: a deletion must stay announced
long enough for every client to see it, without the set growing without bound.

/chat/recent is token-gated, so these use viewer_client; the moderation route
additionally needs the proxy header Traefik stamps.
"""

import pytest

ADMIN = {"X-Admin-Proxy": "test-proxy-secret"}


@pytest.fixture(autouse=True)
def _clean_chat(app_module):
    """Chat state is module-level and survives between tests — reset it so the
    ring-eviction test can't leak a full buffer into its neighbours."""
    with app_module.chat_lock:
        app_module.chat_messages.clear()
        app_module.chat_deleted_ids.clear()
        app_module.chat_evicted_max_id = 0
    yield


def _send(app_module, text="x"):
    """Append straight to the ring, bypassing /chat/send's per-IP rate limit —
    these tests need a few hundred messages."""
    with app_module.chat_lock:
        mid = app_module.chat_next_id
        app_module.chat_next_id += 1
        if len(app_module.chat_messages) == app_module.CHAT_BUFFER_SIZE:
            app_module.chat_evicted_max_id = max(
                app_module.chat_evicted_max_id, app_module.chat_messages[0]["id"])
        app_module.chat_messages.append(
            {"id": mid, "ts": 0, "sid": "s", "name": "n", "text": text})
    return mid


def test_deleting_the_oldest_message_still_announces_it(viewer_client, app_module):
    """The regression: the trim floored on the oldest LIVE id, and deleting the
    oldest message raises that floor past the id just deleted — pruning the
    announcement in the same request that created it, so a caught-up client
    kept painting the deleted message until reload."""
    ids = [_send(app_module) for _ in range(3)]
    assert viewer_client.delete(f"/admin/api/chat/{ids[0]}", headers=ADMIN).status_code == 200
    seen = viewer_client.get(f"/chat/recent?since={ids[-1]}").get_json()["deleted_ids"]
    assert ids[0] in seen, seen


def test_deleting_a_middle_message_still_announces_it(viewer_client, app_module):
    """The case that always worked — kept so a future floor change can't fix
    one direction by breaking the other."""
    ids = [_send(app_module) for _ in range(3)]
    assert viewer_client.delete(f"/admin/api/chat/{ids[1]}", headers=ADMIN).status_code == 200
    seen = viewer_client.get(f"/chat/recent?since={ids[-1]}").get_json()["deleted_ids"]
    assert ids[1] in seen, seen


def test_deleted_ids_are_pruned_once_the_ring_evicts_them(viewer_client, app_module):
    """The property the original trim existed for, and the one a naive fix
    would trade away: past the eviction watermark no client can still be
    painting the message, so the set must shed it rather than grow forever."""
    first = _send(app_module)
    assert viewer_client.delete(f"/admin/api/chat/{first}", headers=ADMIN).status_code == 200
    assert first in viewer_client.get("/chat/recent?since=0").get_json()["deleted_ids"]

    for _ in range(app_module.CHAT_BUFFER_SIZE + 5):
        _send(app_module)

    after = viewer_client.get("/chat/recent?since=0").get_json()["deleted_ids"]
    assert first not in after, "deleted id outlived the ring — the set grows forever"
    assert len(after) <= app_module.CHAT_BUFFER_SIZE
