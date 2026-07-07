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
