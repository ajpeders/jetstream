<script>
  import { onMount } from "svelte";

  let collapsed = false;
  let name = "";
  let text = "";
  let sending = false;
  let placeholder = "Type a message...";
  let feedback = "";
  let feedbackKind = "error";
  let feedbackTimer;
  let messages = [];
  let deletedIds = new Set();
  let maxId = 0;
  let panel;
  let messagePane;
  let sid = "";

  function escapeSid() {
    const bytes = crypto.getRandomValues(new Uint8Array(6));
    return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function chatColor(value) {
    let h = 0;
    for (let i = 0; i < value.length; i += 1) {
      h = ((h << 5) - h + value.charCodeAt(i)) | 0;
    }
    return `hsl(${((h % 360) + 360) % 360}, 65%, 60%)`;
  }

  function toggleCollapsed() {
    collapsed = !collapsed;
    localStorage.setItem("jetstream_chat_collapsed", collapsed ? "1" : "0");
  }

  function handleHeaderKeydown(event) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      toggleCollapsed();
    }
  }

  function persistName() {
    name = name.trim().slice(0, 24);
    localStorage.setItem("jetstream_chatname", name);
  }

  function showFeedback(message, kind = "error") {
    feedback = message;
    feedbackKind = kind;
    clearTimeout(feedbackTimer);
    feedbackTimer = setTimeout(() => {
      feedback = "";
    }, 3000);
  }

  async function pollChat() {
    try {
      const response = await fetch(`/chat/recent?since=${maxId}`);
      if (!response.ok) return;

      const payload = await response.json();
      if (typeof payload.max_id === "number") maxId = payload.max_id;

      if (Array.isArray(payload.deleted_ids) && payload.deleted_ids.length) {
        deletedIds = new Set([...deletedIds, ...payload.deleted_ids]);
      }

      if (Array.isArray(payload.messages) && payload.messages.length) {
        const wasNearBottom = messagePane
          ? messagePane.scrollHeight - messagePane.scrollTop - messagePane.clientHeight < 50
          : true;
        messages = [...messages, ...payload.messages].filter((m) => !deletedIds.has(m.id));
        if (wasNearBottom) {
          requestAnimationFrame(() => {
            if (messagePane) messagePane.scrollTop = messagePane.scrollHeight;
          });
        }
      } else if (deletedIds.size) {
        messages = messages.filter((m) => !deletedIds.has(m.id));
      }
    } catch {
      // Chat is best-effort; playback should never care if polling fails.
    }
  }

  async function sendMessage() {
    const message = text.trim();
    if (!message || sending) return;

    sending = true;
    try {
      const response = await fetch("/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, sid, name: name.trim() })
      });

      if (response.ok) {
        text = "";
        showFeedback("Message sent.", "success");
        await pollChat();
      } else {
        const payload = await response.json().catch(() => ({}));
        const previous = placeholder;
        let message;
        if (payload.error === "muted") {
          const mins = Math.max(1, Math.ceil((payload.remaining || 0) / 60));
          message = `You are muted for about ${mins}m.`;
        } else if (payload.error === "rate limited") {
          message = "Slow down before sending another message.";
        } else {
          message = payload.error || "Message could not be sent.";
        }
        placeholder = message;
        showFeedback(message);
        setTimeout(() => {
          placeholder = previous;
        }, 1600);
      }
    } catch {
      const previous = placeholder;
      placeholder = "Message could not be sent.";
      showFeedback("Message could not be sent.");
      setTimeout(() => {
        placeholder = previous;
      }, 1600);
    } finally {
      sending = false;
    }
  }

  onMount(() => {
    sid = localStorage.getItem("jetstream_chatsid") || escapeSid();
    localStorage.setItem("jetstream_chatsid", sid);
    name = localStorage.getItem("jetstream_chatname") || "";
    collapsed = localStorage.getItem("jetstream_chat_collapsed") === "1";

    pollChat();
    const timer = setInterval(pollChat, 2500);
    return () => {
      clearInterval(timer);
      clearTimeout(feedbackTimer);
    };
  });
</script>

<section id="chat" class:collapsed bind:this={panel}>
  <div
    id="chat-header"
    role="button"
    tabindex="0"
    aria-expanded={!collapsed}
    aria-controls="chat-messages chat-form"
    on:click={toggleCollapsed}
    on:keydown={handleHeaderKeydown}
  >
    <div class="chat-title">
      <h2>Chat</h2>
    </div>
    <div class="chat-meta">
      <span class="chat-count">{messages.length} {messages.length === 1 ? "message" : "messages"}</span>
      <span class="toggle" aria-hidden="true">{collapsed ? "+" : "−"}</span>
    </div>
  </div>

  <div id="chat-messages" bind:this={messagePane} aria-live="polite" aria-label="Chat messages">
    {#if messages.length === 0}
      <div class="empty">
        <strong>No messages yet</strong>
        <span>Start the conversation when you are ready.</span>
      </div>
    {:else}
      {#each messages as message (message.id)}
        <div class:self={message.sid === sid} class="chat-msg" data-id={message.id}>
          <span class="chat-dot" style:background={chatColor(message.sid)}></span>
          <span class="chat-name">{message.name || "anonymous"}</span>
          <span class="chat-text">{message.text}</span>
        </div>
      {/each}
    {/if}
  </div>

  <form id="chat-form" autocomplete="off" on:submit|preventDefault={sendMessage}>
    <input
      id="chat-name-input"
      type="text"
      maxlength="24"
      placeholder="name"
      aria-label="Display name (optional, anonymous if blank)"
      bind:value={name}
      on:change={persistName}
    >
    <input
      id="chat-input"
      type="text"
      maxlength="500"
      {placeholder}
      aria-label="Chat message"
      aria-describedby={feedback ? "chat-feedback" : undefined}
      bind:value={text}
      disabled={sending}
    >
    <button id="chat-send" type="submit" disabled={sending || !text.trim()} aria-busy={sending}>
      {sending ? "Sending..." : "Send"}
    </button>
  </form>

  {#if feedback}
    <div id="chat-feedback" class:success={feedbackKind === "success"} role={feedbackKind === "success" ? "status" : "alert"}>
      {feedback}
    </div>
  {/if}
</section>
