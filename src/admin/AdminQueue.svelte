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

<style>
  #queue-panel .head {
    align-items: center;
    gap: 0.7rem;
  }

  #queue-panel .queue-heading {
    display: grid;
    gap: 0.1rem;
    flex: 1;
    min-width: 0;
  }

  #queue-panel .queue-heading h2 {
    line-height: 1.1;
  }

  #queue-panel .queue-subtitle {
    color: var(--js-faint, #697386);
    font-size: 0.76rem;
    font-weight: 600;
    letter-spacing: 0;
    text-transform: none;
  }

  #queue-panel #queue-list {
    display: grid;
    gap: 0.45rem;
  }

  #queue-panel #queue-list .queue-row {
    display: grid;
    grid-template-columns: 1.1rem 1.85rem 34px minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 0.6rem;
    min-height: 58px;
    padding: 0.52rem 0.62rem;
    border: 1px solid var(--js-line, rgba(148, 163, 184, 0.16)) !important;
    background: rgba(15, 23, 42, 0.42);
    cursor: grab;
  }

  #queue-panel #queue-list .queue-row:active {
    cursor: grabbing;
  }

  #queue-panel #queue-list .queue-row.dragging {
    opacity: 0.45;
  }

  #queue-panel #queue-list .queue-row.drag-over {
    border-color: rgba(56, 189, 248, 0.64) !important;
    box-shadow: inset 3px 0 0 rgba(56, 189, 248, 0.82), 0 10px 28px rgba(8, 13, 20, 0.28);
  }

  #queue-panel .drag-handle {
    color: var(--js-faint, #697386);
    cursor: grab;
    font-size: 0.9rem;
    letter-spacing: -0.14em;
    line-height: 1;
    opacity: 0.7;
    user-select: none;
  }

  #queue-panel .queue-row:hover .drag-handle {
    color: var(--js-muted, #9ca3af);
    opacity: 1;
  }

  #queue-panel .pos {
    align-items: center;
    background: rgba(148, 163, 184, 0.09);
    border: 1px solid var(--js-line, rgba(148, 163, 184, 0.16));
    border-radius: 999px;
    display: inline-flex;
    font-size: 0.74rem;
    font-weight: 800;
    height: 1.65rem;
    justify-content: center;
    min-width: 1.65rem;
    padding: 0 0.3rem;
    text-align: center;
  }

  #queue-panel .qthumb {
    height: 48px;
    width: 34px;
  }

  #queue-panel .qthumb > span {
    color: var(--js-faint, #697386);
    font-size: 0.9rem;
    font-weight: 850;
  }

  #queue-panel .qmain {
    display: grid;
    gap: 0.25rem;
    min-width: 0;
  }

  #queue-panel .qtitle {
    display: block;
    font-size: 0.91rem;
    font-weight: 760;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Chips + source path sit on one row beneath the title — keeps the row
     to a fixed column count so the action cluster can't be pushed off-panel. */
  #queue-panel .qmeta {
    align-items: center;
    display: flex;
    gap: 0.4rem;
    min-width: 0;
  }

  #queue-panel .qref {
    color: var(--js-faint, #697386);
    flex: 1;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.68rem;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  #queue-panel .qdur {
    background: rgba(148, 163, 184, 0.08);
    border: 1px solid var(--js-line, rgba(148, 163, 184, 0.16));
    border-radius: 999px;
    min-width: 3.7rem;
    padding: 0.15rem 0.46rem;
  }

  #queue-panel .qdur:empty {
    display: none;
  }

  #queue-panel .qdur.live {
    background: rgba(239, 68, 68, 0.13);
    border-color: rgba(239, 68, 68, 0.3);
    color: #fca5a5 !important;
  }

  #queue-panel .queue-actions {
    display: flex;
    gap: 0.3rem;
    justify-content: end;
  }

  #queue-panel .queue-actions .qbtn {
    align-items: center;
    display: inline-flex;
    flex: 0 0 auto;
    height: 32px;
    justify-content: center;
    min-height: 32px;
    min-width: 32px;
    padding: 0 !important;
    width: 32px;
  }

  #queue-panel .queue-actions .qbtn svg {
    height: 17px;
    width: 17px;
  }

  #queue-panel .queue-state {
    align-items: center;
    background: rgba(15, 23, 42, 0.34);
    border: 1px dashed var(--js-line-strong, rgba(148, 163, 184, 0.24)) !important;
    border-radius: 8px;
    display: grid;
    gap: 0.7rem;
    grid-template-columns: 2.25rem minmax(0, 1fr) auto;
    padding: 1rem;
  }

  #queue-panel .queue-state strong,
  #queue-panel .queue-state small {
    display: block;
  }

  #queue-panel .queue-state strong {
    color: var(--js-text, #e5e7eb);
    font-size: 0.92rem;
    margin-bottom: 0.16rem;
  }

  #queue-panel .queue-state small {
    color: var(--js-faint, #697386);
    font-size: 0.8rem;
    line-height: 1.4;
  }

  #queue-panel .state-icon {
    align-items: center;
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.26);
    border-radius: 999px;
    color: #7dd3fc;
    display: inline-flex;
    font-size: 0.95rem;
    font-weight: 850;
    height: 2.25rem;
    justify-content: center;
    width: 2.25rem;
  }

  #queue-panel .queue-state[aria-live] .state-icon:empty::before {
    animation: queuePulse 0.8s ease-in-out infinite alternate;
    background: currentColor;
    border-radius: 999px;
    content: "";
    height: 0.55rem;
    width: 0.55rem;
  }

  #queue-panel .error-state .state-icon {
    background: rgba(239, 68, 68, 0.12);
    border-color: rgba(239, 68, 68, 0.32);
    color: #fca5a5;
  }

  @keyframes queuePulse {
    from {
      opacity: 0.35;
      transform: scale(0.8);
    }
    to {
      opacity: 1;
      transform: scale(1.08);
    }
  }

  @media (max-width: 760px) {
    #queue-panel #queue-list .queue-row {
      grid-template-columns: 1rem 1.8rem 34px minmax(0, 1fr) auto;
      gap: 0.55rem;
      padding: 0.62rem;
    }

    /* Duration moves under the title on narrow screens; the action cluster
       gets its own full-width row so the 44px touch targets have room. */
    #queue-panel .queue-row .qdur {
      display: none;
    }

    #queue-panel .queue-actions {
      grid-column: 1 / -1;
      justify-content: stretch;
      padding-left: calc(1rem + 1.8rem + 34px + 1.65rem);
    }

    #queue-panel .queue-actions .qbtn {
      flex: 1;
      height: 44px;
      width: auto;
    }

    #queue-panel .queue-state {
      grid-template-columns: 2.25rem minmax(0, 1fr);
    }

    #queue-panel .queue-state button {
      grid-column: 1 / -1;
      justify-self: start;
    }
  }
</style>
