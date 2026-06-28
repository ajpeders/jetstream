<script>
  import { onMount } from "svelte";

  const api = location.pathname.startsWith("/controls") ? "/api/control" : "/admin/api";

  let items = [];
  let loading = true;
  let error = "";
  let pending = null;

  function toast(message) {
    if (typeof window.jetstreamShowToast === "function") window.jetstreamShowToast(message);
  }

  function titleOf(item) {
    const source = item.source || {};
    return source.title || source.ref || "Untitled";
  }

  function sourceOf(item) {
    const source = item.source || {};
    if (source.type === "file") return "File";
    if (source.type === "url") return source.is_live ? "Live URL" : "URL";
    return "Source";
  }

  function timeAgo(ts) {
    if (!ts) return "";
    const delta = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (delta < 60) return `${delta}s ago`;
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    return `${Math.floor(delta / 86400)}d ago`;
  }

  function fmtDur(sec) {
    if (!sec || !Number.isFinite(sec) || sec < 0) return "";
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  async function loadRecent() {
    try {
      const response = await fetch(`${api}/recent`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      items = await response.json();
      error = "";
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function requeue(index) {
    pending = index;
    try {
      const response = await fetch(`${api}/recent/${index}/queue`, { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      toast(`Requeued: ${payload.first_title || titleOf(items[index])}`);
      if (window.jetstreamQueue?.reload) await window.jetstreamQueue.reload();
    } catch (err) {
      toast(`Requeue failed: ${err.message}`);
    } finally {
      pending = null;
    }
  }

  onMount(() => {
    loadRecent();
    const timer = setInterval(loadRecent, 15000);
    return () => clearInterval(timer);
  });
</script>

<section id="recent-panel">
  <div class="head">
    <div>
      <h2>Recently played</h2>
      <span class="recent-subtitle">{items.length ? `${items.length} saved` : "Nothing yet"}</span>
    </div>
    <button type="button" on:click={loadRecent} title="Refresh recently played">Refresh</button>
  </div>

  <ul id="recent-list" aria-busy={loading}>
    {#if loading}
      <li class="recent-state">Loading recently played...</li>
    {:else if error}
      <li class="recent-state error-state">
        <span>Failed to load: {error}</span>
        <button type="button" on:click={loadRecent}>Retry</button>
      </li>
    {:else if !items.length}
      <li class="recent-state">
        <strong>No recently played items</strong>
        <span>Played files and URLs will show up here for quick requeue.</span>
      </li>
    {:else}
      {#each items.slice(0, 8) as item, index}
        {@const source = item.source || {}}
        <li class="recent-row">
          <span class="recent-thumb">
            {#if source.type === "file" && source.ref}
              <img loading="lazy" alt="" src={`/poster?path=${encodeURIComponent(source.ref)}`}>
            {:else}
              <span aria-hidden="true">U</span>
            {/if}
          </span>
          <span class="recent-main">
            <span class="recent-title" title={source.ref}>{titleOf(item)}</span>
            <span class="recent-meta">
              <span>{sourceOf(item)}</span>
              {#if source.duration}<span>{fmtDur(source.duration)}</span>{/if}
              <span>{timeAgo(item.ts)}</span>
            </span>
          </span>
          <button type="button" on:click={() => requeue(index)} disabled={pending === index} title={`Requeue ${titleOf(item)}`}>
            {pending === index ? "Adding..." : "Requeue"}
          </button>
        </li>
      {/each}
    {/if}
  </ul>
</section>

<style>
  #recent-panel {
    background: rgba(17, 24, 33, 0.94);
    border: 1px solid var(--js-line, rgba(148, 163, 184, 0.18));
    border-radius: var(--js-radius, 8px);
    box-shadow: 0 12px 34px rgba(0, 0, 0, 0.18), var(--js-inner, inset 0 1px rgba(255,255,255,.05));
    padding: 0.9rem 1rem;
  }

  #recent-panel .head {
    align-items: center;
    display: flex;
    gap: 0.7rem;
    justify-content: space-between;
    margin-bottom: 0.55rem;
  }

  #recent-panel h2 {
    color: var(--js-text, #e7edf5);
    font-size: 0.78rem;
    font-weight: 850;
    letter-spacing: 0.08em;
    margin: 0;
    text-transform: uppercase;
  }

  .recent-subtitle {
    color: var(--js-faint, #657386);
    display: block;
    font-size: 0.76rem;
    font-weight: 600;
    margin-top: 0.14rem;
  }

  #recent-list {
    display: grid;
    gap: 0.38rem;
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .recent-row {
    align-items: center;
    background: rgba(8, 13, 20, 0.18);
    border: 1px solid var(--js-line, rgba(148, 163, 184, 0.18));
    border-radius: 7px;
    display: grid;
    gap: 0.55rem;
    grid-template-columns: 28px minmax(0, 1fr) auto;
    min-height: 44px;
    padding: 0.42rem 0.5rem;
  }

  .recent-thumb {
    align-items: center;
    background: var(--js-panel-3, #1b2633);
    border-radius: 6px;
    display: flex;
    flex: 0 0 28px;
    height: 38px;
    justify-content: center;
    overflow: hidden;
    max-height: 38px;
    max-width: 28px;
    min-height: 38px;
    min-width: 28px;
    width: 28px;
  }

  .recent-thumb img {
    display: block;
    flex: 0 0 auto;
    height: 38px;
    max-height: 38px;
    max-width: 28px;
    min-height: 38px;
    min-width: 28px;
    object-fit: cover;
    width: 28px;
  }

  .recent-thumb span {
    color: var(--js-faint, #657386);
    font-weight: 850;
  }

  .recent-main {
    display: grid;
    gap: 0.15rem;
    min-width: 0;
  }

  .recent-title {
    color: var(--js-text, #e7edf5);
    font-size: 0.9rem;
    font-weight: 760;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .recent-meta {
    color: var(--js-faint, #657386);
    display: flex;
    flex-wrap: wrap;
    font-size: 0.72rem;
    gap: 0.45rem;
  }

  .recent-state {
    border: 1px dashed var(--js-line-strong, rgba(148, 163, 184, 0.3));
    border-radius: 7px;
    color: var(--js-faint, #657386);
    display: grid;
    gap: 0.25rem;
    padding: 0.9rem;
  }

  .recent-state strong { color: var(--js-text, #e7edf5); }
  .error-state { color: #fca5a5; }

  @media (max-width: 760px) {
    .recent-row { grid-template-columns: 28px minmax(0, 1fr); }
    .recent-row button { grid-column: 1 / -1; }
  }
</style>
