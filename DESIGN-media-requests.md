# Design — media requests (replacing Overseerr)

Status: **phase 2 shipped.** Decided scope: full Overseerr replacement; requesting requires a **user account** (`js_user`), not an invite token. Current main has the separate persisted store, account/admin endpoints, host admin panel skeleton, and read-only Radarr/Sonarr lookup search in `/library`. Arr writes are still a later phase.

## The core distinction

jetstream already has a thing called a "request", and it is **not** this one:

| | Existing (`/api/request`, #27) | New (this design) |
|---|---|---|
| Means | "play this file **we already have**" | "acquire this thing **we don't have**" |
| Input | a path from the viewer library browse | a TMDB/TVDB id from a discovery search |
| Approval does | adds to the playback queue | pushes to Sonarr/Radarr to download |
| Lifetime | minutes–hours, 7-day TTL | days; ends when the file lands |
| Store | `/data/requests.json` | `/data/media_requests.json` (separate) |

**These must stay separate** — same word, opposite direction. Merging them into one queue makes both ambiguous, and the admin panel would show two things that need completely different verbs ("Approve → plays now" vs "Approve → starts a 40 GB download"). Separate store, separate panel, separate route prefix (`/api/media/*`).

## What already exists and gets reused

Genuinely most of the plumbing:

- **arr auth** — `_read_arr_api_key()` pulls `<ApiKey>` from the read-only-mounted `config.xml`; requests go out as `X-Api-Key` over `urllib`. Already proven against both services.
- **arr polling** — `_arr_refresh_loop()` already hits `/api/v3/series` and `/api/v3/movie` every `ARR_REFRESH_SECS` and never raises on failure. The new "do we already have this?" and "has it downloaded yet?" checks are an extension of this existing poll, not new infrastructure.
- **Accounts + tiers** — `_session_user()`, the users store, and the admin Users panel are exactly Overseerr's user management.
- **Request-queue idiom** — `/data/requests.json` + lock + `_save_X` + admin panel + approve/deny + TTL sweep is a working template to copy, not invent.
- **Poster proxy** — `/poster` already caches arr cover art. Discovery results need the same for TMDB art.
- **Rate limiting** — `_ip_rate_check(..., record=False)` peek mode already solves "don't charge failures against the budget".

## What's actually missing

1. **Discovery** — browsing/searching media you *don't* own.
2. **Write access to arr** — everything today is read-only.
3. **Availability tracking** — requested → downloading → ready.
4. **Quotas** — an approval gate is not enough on its own; see Risks.

## Discovery: TMDB or arr lookup?

Both, for different jobs — and this matters because it decides whether a TMDB key is required at all.

- **Search by title** needs no TMDB key. `GET /api/v3/movie/lookup?term=` (Radarr) and `GET /api/v3/series/lookup?term=` (Sonarr) already proxy TMDB/TVDB and return arr-shaped objects that can be POSTed straight back to add. Fewer moving parts, no extra secret, and the result is already in the format the add step wants.
- **Browse / trending / popular / recommendations** has no arr equivalent and *does* need a TMDB API key.

**Recommendation:** build search-first on arr lookup (phase 2), add TMDB browse as a distinct later phase (phase 5) so a missing TMDB key degrades to "search works, browse tab hidden" rather than breaking the feature.

Note this reverses a closed ROADMAP decision — TMDB was dropped as "not worth the friction for a personal co-watching setup". That was about enriching the *existing* library; wanting Overseerr's discovery is a different requirement, so the reversal is deliberate rather than an oversight.

## Data model

`/data/media_requests.json`, `media_requests_lock`, written via `_atomic_write_json`:

```
{
  "id": "mr_<token_urlsafe(8)>",
  "user_id": "u_…",            # who asked — accounts only, so always attributable
  "username": "dave",          # denormalised so a deleted user still renders
  "kind": "movie" | "series",
  "tmdb_id": 603, "tvdb_id": null,
  "title": "The Matrix", "year": 1999,
  "poster_url": "https://image.tmdb.org/…",
  "status": "pending",
  "requested_at": 1785…, "decided_at": null, "decided_by": null,
  "reject_reason": null,
  "arr_id": null,              # Radarr movieId / Sonarr seriesId once added
  "seasons": null,             # series only: [1,2] or null for all
  "quality_profile_id": null,  # resolved at approval, not request time
  "last_seen_available": null
}
```

Status machine — one direction, no loops:

```
pending ──approve──> approved ──pushed to arr──> downloading ──file lands──> available
   │                                                  │
   └──reject──> rejected                              └──arr error / removed──> failed
```

`downloading → available` is decided by the arr poll (below), never by the client.

## Availability tracking

Extend the existing refresh loop rather than adding a second one:

- The inventory poll already fetches every series/movie. Additionally index **`tmdbId` / `tvdbId` → `{hasFile, sizeOnDisk, statistics}`**. That single index answers both open questions:
  - *"already in the library?"* — used to grey out discovery results, so nobody requests what you already have.
  - *"has it downloaded?"* — flips `downloading → available`.
- For in-flight progress, `GET /api/v3/queue` on each service gives percent-complete and ETA. Poll this on a **shorter** interval than the inventory (say 60 s vs 30 min) but only while at least one request is in `downloading` — no in-flight requests, no polling.

## Endpoints

All gated to account holders by `_gate_viewer_routes` (add `/api/media/` to the session-required prefixes; `/admin/api/media/` follows the admin path).

| Route | Who | Does |
|---|---|---|
| `GET /api/media/search?q=` | user | arr lookup, merged movie+series, annotated `already_have` / `already_requested` |
| `POST /api/media/request` | user | `{kind, tmdb_id\|tvdb_id, seasons?}` → new `pending`. 409 if already have / already requested |
| `GET /api/media/requests` | user | *their own* requests + status |
| `DELETE /api/media/requests/<id>` | user | cancel, only while `pending`, only their own |
| `GET /api/media/discover?tab=` | user | TMDB trending/popular — **phase 5**, 404s until then |
| `GET /admin/api/media/requests` | admin | all requests, newest first |
| `POST /admin/api/media/requests/<id>/approve` | admin | `{quality_profile_id?, root_folder?}` → push to arr, `approved`→`downloading` |
| `POST /admin/api/media/requests/<id>/reject` | admin | `{reason?}` |
| `GET /admin/api/media/arr` | admin | quality profiles + root folders, for the approve dialog |

## UI

- **`/library`** gains a "Request something new" entry point — search box → results grid (poster, year, overview) → Request button. Results already in the library link straight to playing them instead.
- **`/home`** hub gains "My requests" with status chips.
- **`/admin`** gains a Media Requests panel: approve (with quality-profile + root-folder pickers, defaulted) / reject with reason / bulk clear of finished ones. Mirrors the existing Requests panel idiom.

## Risks and the decisions they force

1. **Approval spends real resources.** Approving starts a download that consumes disk and bandwidth — unlike every other approve button in this app, it isn't cheaply reversible. Hence: accounts-only (attribution), an explicit approve step (never auto-approve), and **per-user quotas** (`MEDIA_REQUEST_QUOTA_PER_WEEK`, default ~5). An approval gate alone means *you* become the rate limiter.
2. **Writing to arr is the first non-read-only integration.** A bug here mutates a service jetstream doesn't own. Every write goes through one narrow `_arr_post()` helper with a hard allowlist of endpoints, and approval is idempotent — re-approving must not add twice (check `arr_id` first, and handle arr's own "already exists" 400).
3. **arr being down must not break the page.** Existing behaviour (log to stderr, degrade) is the template: search returns empty with a notice, approve returns 503 and leaves status `approved` for retry — never a half-written state.
4. **Disk.** Nothing here checks free space. At minimum surface arr's root-folder `freeSpace` in the approve dialog so approving a 4K remux while 80 GB remain is a visible choice.
5. **Deleted users.** `username` is denormalised into the record so history survives account deletion; requests from a deleted user stay visible to admin but stop counting against any quota.

## Phasing

Each phase is independently shippable and useful on its own:

1. **Store + admin panel skeleton.** **Shipped.** Data model, lock, persisted `/data/media_requests.json`, `GET/POST/DELETE` for requests, admin list/approve/reject that only changes *status* — no arr writes at all. Fully testable with zero risk to arr.
2. **arr lookup search + request flow.** **Shipped.** `/api/media/search` merges Radarr movie lookup and Sonarr series lookup, annotates `already_have` from the extended inventory index and `already_requested` from `/data/media_requests.json`, and `/library` now has a mobile-friendly "Request something new" panel. Still no writes.
3. **arr writes.** `_arr_post()`, approve actually adds to Radarr/Sonarr, quality-profile/root-folder pickers, idempotency.
4. **Availability tracking.** Extended inventory index, conditional queue polling, `downloading → available`, "ready to watch" notice that deep-links into `/library`.
5. **TMDB discovery.** Trending/popular/recommendations tabs; hidden entirely when no key is configured.

Phase 1 is the right place to start: it is the whole shape of the feature with none of the danger, and it makes phases 3–4 reviewable against something real.

## Open questions for later phases

- Series requests: whole show, or per-season? (Model has `seasons`; UI can defer to "all" in phase 2.)
- Notify on availability, or leave it to the "ready to watch" row? (No notification transport exists today.)
- 4K vs 1080p as a user-visible choice, or always admin's default profile?
