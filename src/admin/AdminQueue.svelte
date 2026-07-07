<script>
  import { onMount } from "svelte";

  const api = location.pathname.startsWith("/controls") ? "/api/control" : "/admin/api";

  let items = [];
  let error = "";
  let loading = true;
  let dragSrcIndex = null;
  let dragOverIndex = null;

  function toast(message) {
    if (typeof window.jetstreamShowToast === "function") {
      window.jetstreamShowToast(message);
    }
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

  function chipForItem(item) {
    if (item.type === "file") return { cls: "", text: "FILE" };
    const ref = item.ref || "";
    if (/^https?:\/\/(www\.|m\.)?(youtube\.com|youtu\.be)\b/i.test(ref)) {
      return { cls: "yt", text: "YOUTUBE" };
    }
    return { cls: "url", text: "URL" };
  }

  function itemLabel(item) {
    return item.title || item.ref || "Untitled item";
  }

  async function loadQueue() {
    try {
      const response = await fetch(`${api}/queue`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      items = await response.json();
      error = "";
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function removeItem(index) {
    try {
      await fetch(`${api}/queue/${index}`, { method: "DELETE" });
      await loadQueue();
    } catch (err) {
      toast(`Failed: ${err.message}`);
    }
  }

  async function moveItem(index, body) {
    try {
      await fetch(`${api}/queue/${index}/move`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      await loadQueue();
    } catch (err) {
      toast(`Move failed: ${err.message}`);
    }
  }

  async function clearQueue() {
    if (!confirm("Clear the entire queue?")) return;
    try {
      await fetch(`${api}/queue/clear`, { method: "POST" });
      toast("Queue cleared");
      await loadQueue();
    } catch (err) {
      toast(`Failed: ${err.message}`);
    }
  }

  async function shuffleQueue() {
    try {
      const response = await fetch(`${api}/queue/shuffle`, { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      toast(`Shuffled ${payload.queue_length} item${payload.queue_length === 1 ? "" : "s"}`);
      await loadQueue();
    } catch (err) {
      toast(`Shuffle failed: ${err.message}`);
    }
  }

  function onDragStart(event, index) {
    if (event.target.closest("button")) {
      event.preventDefault();
      return;
    }
    dragSrcIndex = index;
    event.dataTransfer.effectAllowed = "move";
    try {
      event.dataTransfer.setData("text/plain", String(index));
    } catch {
      // Firefox only needs some dataTransfer payload; failures are harmless.
    }
  }

  function onDrop(event, index) {
    event.preventDefault();
    const src = dragSrcIndex;
    dragSrcIndex = null;
    dragOverIndex = null;
    if (src == null || src === index) return;
    moveItem(src, { to: index });
  }

  function onDragEnd() {
    dragSrcIndex = null;
    dragOverIndex = null;
  }

  onMount(() => {
    window.jetstreamQueue = { reload: loadQueue };
    loadQueue();
    const timer = setInterval(loadQueue, 5000);
    return () => {
      clearInterval(timer);
      if (window.jetstreamQueue?.reload === loadQueue) delete window.jetstreamQueue;
    };
  });
</script>

<div id="queue-panel">
  <div class="head">
    <div class="queue-heading">
      <h2>Up next</h2>
      <span class="queue-subtitle">{items.length ? `${items.length} queued` : "Ready for media"}</span>
    </div>
    <span class="badge" id="queue-count" aria-label={`${items.length} queue items`}>{items.length}</span>
    <button id="queue-shuffle" type="button" on:click={shuffleQueue} disabled={!items.length} title="Shuffle the queue">Shuffle</button>
    <button id="queue-clear" type="button" on:click={clearQueue} disabled={!items.length} title="Clear every queued item">Clear</button>
  </div>

  <ul id="queue-list" aria-busy={loading}>
    {#if loading}
      <li class="empty-q queue-state" aria-live="polite">
        <span class="state-icon" aria-hidden="true"></span>
        <span>
          <strong>Loading queue</strong>
          <small>Checking what is coming up next.</small>
        </span>
      </li>
    {:else if error}
      <li class="empty-q queue-state error-state" aria-live="polite">
        <span class="state-icon" aria-hidden="true">!</span>
        <span>
          <strong>Queue unavailable</strong>
          <small>Failed to load queue: {error}</small>
        </span>
        <button type="button" on:click={loadQueue} title="Retry loading the queue">Retry</button>
      </li>
    {:else if !items.length}
      <li class="empty-q queue-state">
        <span class="state-icon" aria-hidden="true">+</span>
        <span>
          <strong>Queue is empty</strong>
          <small>Add a file from the library or paste a URL above. New items will play after the current stream.</small>
        </span>
      </li>
    {:else}
      {#each items as item, index (item.ref + ":" + index)}
        {@const chip = chipForItem(item)}
        {@const hasSubs = item.subtitle_idx !== null && item.subtitle_idx !== undefined}
        {@const label = itemLabel(item)}
        <li
          class="queue-row"
          draggable="true"
          class:dragging={dragSrcIndex === index}
          class:drag-over={dragOverIndex === index && dragSrcIndex !== index}
          aria-label={`Queue item ${index + 1}: ${label}`}
          on:dragstart={(event) => onDragStart(event, index)}
          on:dragend={onDragEnd}
          on:dragenter={() => (dragOverIndex = index)}
          on:dragover|preventDefault
          on:dragleave={() => {
            if (dragOverIndex === index) dragOverIndex = null;
          }}
          on:drop={(event) => onDrop(event, index)}
        >
          <span class="drag-handle" title="Drag to reorder" aria-hidden="true">⋮⋮</span>
          <span class="pos" aria-label={`Position ${index + 1}`}>{index + 1}</span>
          <span class="qthumb">
            {#if item.type === "file" && item.ref}
              <img loading="lazy" alt="" src={`/poster?path=${encodeURIComponent(item.ref)}`} on:load={(event) => event.currentTarget.classList.add("loaded")}>
            {:else}
              <span aria-hidden="true">{chip.text.slice(0, 1)}</span>
            {/if}
          </span>
          <span class="qmain">
            <span class="qtitle" title={item.ref}>{label}</span>
            <span class="qmeta">
              <span class={`qchip ${chip.cls}`} title={`Source: ${chip.text.toLowerCase()}`}>{chip.text}</span>
              {#if hasSubs}
                <span class="qcc" title="Subtitles will be burned in">CC</span>
              {/if}
              {#if item.ref && item.title && item.ref !== item.title}
                <span class="qref" title={item.ref}>{item.ref}</span>
              {/if}
            </span>
          </span>
          {#if item.is_live}
            <span class="qdur live">LIVE</span>
          {:else}
            <span class="qdur">{fmtDur(item.duration)}</span>
          {/if}
          <span class="queue-actions" aria-label={`Actions for ${label}`}>
            <button type="button" class="qbtn" disabled={index === 0} title="Move this item up" aria-label={`Move ${label} up`} on:click={() => moveItem(index, { direction: "up" })}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 14l6-6 6 6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
            <button type="button" class="qbtn" disabled={index === items.length - 1} title="Move this item down" aria-label={`Move ${label} down`} on:click={() => moveItem(index, { direction: "down" })}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 10l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
            <button type="button" class="qbtn danger" title="Remove this item from the queue" aria-label={`Remove ${label} from the queue`} on:click={() => removeItem(index)}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
            </button>
          </span>
        </li>
      {/each}
    {/if}
  </ul>
</div>
