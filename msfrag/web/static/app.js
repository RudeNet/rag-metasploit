(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const md = window.renderMarkdown, esc = window.escapeHtml;
  const messagesEl = $("messages"), input = $("input");

  let chats = [], currentId = null, generating = false, lastContext = null;

  // ---------- preferences (per browser) ----------
  const PREF_DEFAULTS = { theme: "system", fontSize: "medium", enterSends: true, expandSteps: false, showStats: true };
  let prefs = { ...PREF_DEFAULTS };
  try { prefs = { ...PREF_DEFAULTS, ...JSON.parse(localStorage.getItem("msfrag.prefs") || "{}") }; } catch {}
  function savePrefs() { try { localStorage.setItem("msfrag.prefs", JSON.stringify(prefs)); } catch {} }
  function applyPrefs() {
    const root = document.documentElement;
    if (prefs.theme === "system") delete root.dataset.theme; else root.dataset.theme = prefs.theme;
    root.dataset.fontsize = prefs.fontSize;
    document.body.classList.toggle("no-stats", !prefs.showStats);
    $("sendHint").textContent = prefs.enterSends ? "Enter to send · Shift+Enter for new line"
                                                 : "Ctrl+Enter to send · Enter for new line";
  }

  // ---------- api ----------
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  }

  // ---------- sidebar ----------
  async function loadChats() {
    chats = await api("/api/chats");
    const list = $("chatList");
    list.innerHTML = "";
    for (const c of chats) {
      const item = el("div", "chat-item" + (c.id === currentId ? " active" : ""));
      const a = el("a"); a.href = "#" + c.id; a.title = c.title;
      a.innerHTML = `<span class="t-row"><span class="title">${esc(c.title)}</span><span class="time">${ago(c.updated)}</span></span>` +
        (c.preview ? `<span class="preview">${esc(c.preview)}</span>` : "");
      const del = el("button", "del", "✕"); del.title = "Delete chat";
      del.onclick = async (e) => {
        e.preventDefault();
        if (!confirm("Delete this chat?")) return;
        await api(`/api/chats/${c.id}`, { method: "DELETE" });
        if (c.id === currentId) newChat(); else loadChats();
      };
      item.append(a, del);
      list.append(item);
    }
  }

  async function loadNotes() {
    const list = $("noteList");
    let notesList = [];
    try { notesList = await api("/api/notes"); } catch { return; }
    list.innerHTML = notesList.length ? "" : '<p class="list-empty">No notes yet. Use “Summarize” on a chat.</p>';
    for (const n of notesList) {
      const item = el("div", "chat-item note-item");
      const a = el("a"); a.href = "#"; a.title = n.title;
      a.innerHTML = `<span class="t-row"><span class="title">${esc(n.title.replace(/^Notes: /, ""))}</span><span class="time">${ago(n.created)}</span></span>` +
        `<span class="preview">~${Math.ceil(n.size / 4).toLocaleString()} tokens</span>`;
      a.onclick = (e) => { e.preventDefault(); openNote(n.id); };
      item.append(a); list.append(item);
    }
  }

  const NOTE_LIMIT = 3;
  let currentNotes = [];   // [{id, title}] attached to the open chat

  async function setChatNotes(ids) {
    const chat = await api(`/api/chats/${currentId}/notes`, { method: "PUT", body: JSON.stringify({ notes: ids }) });
    showNoteChips(chat.notes);
    showContext(chat.context);
    return chat;
  }

  function noteEditor(n) {
    // n = existing note, or null for a new one
    const body = $("drawerBody");
    $("drawerTitle").textContent = n ? "Edit note" : "New note";
    body.innerHTML = `
      <div class="note-form">
        <label>Title<input id="noteTitle" maxlength="120" value="${esc(n?.title || "")}" placeholder="e.g. Contract review, project scope, open questions"></label>
        <label>Content <span class="small" id="noteTok"></span>
          <textarea id="noteContent" placeholder="Markdown. Findings, context, definitions, what to look for… The model sees this as background whenever the note is attached to a chat.">${esc(n?.content || "")}</textarea></label>
        <p class="error" id="noteErr" hidden></p>
        <div class="drawer-actions">
          <button class="primary" id="noteSave">Save</button>
          <button id="noteCancel">Cancel</button>
        </div>
      </div>`;
    const ta = $("noteContent");
    const tok = () => { $("noteTok").textContent = `~${Math.ceil(ta.value.length / 4).toLocaleString()} tokens`; };
    ta.oninput = tok; tok();
    $("noteCancel").onclick = () => (n ? openNote(n.id) : closeDrawer());
    $("noteSave").onclick = async () => {
      const payload = { title: $("noteTitle").value, content: ta.value };
      try {
        const saved = n ? await api(`/api/notes/${n.id}`, { method: "PUT", body: JSON.stringify(payload) })
                        : await api("/api/notes", { method: "POST", body: JSON.stringify(payload) });
        loadNotes();
        if (currentId && currentNotes.some((x) => x.id === saved.id)) openChatQuiet();
        openNote(saved.id);
      } catch (e) {
        $("noteErr").hidden = false; $("noteErr").textContent = e.message;
      }
    };
    (n ? ta : $("noteTitle")).focus();
  }

  async function openChatQuiet() {
    // refresh chips + context meter of the open chat without re-rendering messages
    try { const c = await api(`/api/chats/${currentId}`); showNoteChips(c.notes); showContext(c.context); } catch {}
  }

  async function openNote(id) {
    const drawer = $("drawer"), body = $("drawerBody");
    $("drawerTitle").textContent = "Note";
    body.innerHTML = '<p class="small">Loading…</p>';
    drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false");
    try {
      const n = await api(`/api/notes/${id}`);
      const attached = currentNotes.some((x) => x.id === n.id);
      const canAttach = currentId && !attached && currentNotes.length < NOTE_LIMIT;
      body.innerHTML = `<h3 style="margin-top:0">${esc(n.title)}</h3>
        <div class="drawer-actions">
          <button class="primary" id="noteNewChat">New chat with this note</button>
          ${attached ? '<button id="noteDetach">Detach from this chat</button>'
            : currentId ? `<button id="noteAttach" ${canAttach ? "" : "disabled title=\"A chat can have at most 3 notes\""}>Attach to this chat</button>` : ""}
          <button id="noteEdit">✎ Edit</button>
          <button id="noteDownload">⬇ Download</button>
          ${n.source_chat ? '<button id="noteSource">Open source chat</button>' : ""}
          <button class="danger" id="noteDelete">Delete</button>
        </div>
        <p class="small">~${Math.ceil(n.content.length / 4).toLocaleString()} tokens when attached to a chat` +
        `${n.source_chat ? "" : " · written by hand"} · ${new Date(n.created * 1000).toLocaleString()}</p>
        <div class="content">${md(n.content)}</div>`;
      $("noteNewChat").onclick = () => { closeDrawer(); startChatWithNotes([n.id]); };
      $("noteEdit").onclick = () => noteEditor(n);
      $("noteDownload").onclick = () => download(`/api/notes/${n.id}/export`);
      if ($("noteAttach")) $("noteAttach").onclick = async () => {
        try { await setChatNotes([...currentNotes.map((x) => x.id), n.id]); openNote(n.id); } catch (e) { alert(e.message); }
      };
      if ($("noteDetach")) $("noteDetach").onclick = async () => {
        try { await setChatNotes(currentNotes.filter((x) => x.id !== n.id).map((x) => x.id)); openNote(n.id); } catch (e) { alert(e.message); }
      };
      if (n.source_chat) $("noteSource").onclick = () => { closeDrawer(); location.hash = n.source_chat; };
      $("noteDelete").onclick = async () => {
        if (!confirm("Delete this note? Chats that use it will no longer see it.")) return;
        await api(`/api/notes/${n.id}`, { method: "DELETE" });
        closeDrawer(); loadNotes();
        if (currentId) openChatQuiet();
      };
    } catch (e) {
      body.innerHTML = `<p class="error">${esc(String(e.message || e))}</p>`;
    }
  }

  async function startChatWithNotes(noteIds) {
    if (generating) return;
    const c = await api("/api/chats", { method: "POST", body: JSON.stringify({ notes: noteIds }) });
    location.hash = c.id;   // hashchange -> openChat
    await openChat(c.id);
    input.focus();
  }

  async function download(url) {
    const r = await fetch(url);
    if (!r.ok) { alert("Download failed: " + r.statusText); return; }
    const name = (r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || "export.md";
    const a = el("a"); a.href = URL.createObjectURL(await r.blob()); a.download = name;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  // Summarize the current chat into a note; returns the note.
  async function summarizeCurrent(buttons) {
    buttons.forEach((b) => { b.disabled = true; b.dataset.label = b.textContent; b.textContent = "Summarizing…"; });
    try {
      const note = await api(`/api/chats/${currentId}/summarize`, { method: "POST" });
      loadNotes();
      return note;
    } catch (e) {
      alert("Summary failed: " + e.message + (e.message.includes("nothing") ? "" : "\nIs Ollama running?"));
      return null;
    } finally {
      buttons.forEach((b) => { b.disabled = false; b.textContent = b.dataset.label; });
    }
  }

  function showNoteChips(notesList) {
    currentNotes = notesList || [];
    const box = $("noteChips");
    box.innerHTML = "";
    box.hidden = !currentNotes.length;
    for (const n of currentNotes) {
      const chip = el("span", "note-chip");
      const open = el("button", "note-open"); open.textContent = "📎 " + n.title;
      open.title = "Attached note: its content is background context for this chat. Click to view.";
      open.onclick = () => openNote(n.id);
      const x = el("button", "note-x", "✕"); x.title = "Detach from this chat";
      x.onclick = async () => {
        if (generating) return;
        try { await setChatNotes(currentNotes.filter((y) => y.id !== n.id).map((y) => y.id)); } catch (e) { alert(e.message); }
      };
      chip.append(open, x); box.append(chip);
    }
  }

  function ago(ts) {
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "now";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h";
    if (s < 7 * 86400) return Math.floor(s / 86400) + "d";
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function newChat() {
    hideSettingsPage(); hideLibraryPage();
    currentId = null;
    history.replaceState(null, "", location.pathname);
    $("chatTitle").textContent = "New chat";
    messagesEl.innerHTML = "";
    messagesEl.append(emptyEl);
    showContext(null);
    showNoteChips([]);
    $("chatActions").hidden = true;
    loadChats();
    input.focus();
  }

  async function openChat(id) {
    if (generating) return;
    try {
      const chat = await api(`/api/chats/${id}`);
      currentId = id;
      $("chatTitle").textContent = chat.title;
      messagesEl.innerHTML = "";
      renderHistory(chat.messages);
      showContext(chat.context);
      showNoteChips(chat.notes);
      $("chatActions").hidden = !chat.messages.length;
      loadChats();
      scrollBottom(true);
    } catch { newChat(); }
  }

  // ---------- context meter ----------
  // Levels are measured against the usable budget (window minus the space reserved for the answer),
  // because that is where compaction starts.
  const dismissedBanner = new Set();
  function ctxLevel(c, extra = 0) {
    if (!c || !c.limit) return { pct: 0, level: "ok" };
    const budget = c.budget || c.limit;
    const pct = ((c.estimated_tokens || 0) + extra) / budget;
    const trimming = (c.compacted_results || 0) + (c.dropped_turns || 0) > 0;
    return { pct, level: pct >= 0.9 || trimming ? "full" : pct >= 0.7 ? "warn" : "ok" };
  }

  function showContext(c) {
    const chip = $("ctx");
    lastContext = c;
    updateInputTokens();
    if (!c || !c.limit) { chip.hidden = true; $("ctxBanner").hidden = true; return; }
    const used = Math.max(c.prompt_tokens || 0, c.estimated_tokens || 0);
    const { pct, level } = ctxLevel(c);
    chip.hidden = false;
    chip.className = "ctx" + (level === "ok" ? "" : " " + level);
    chip.innerHTML = `<span class="meter"><i style="width:${Math.min(100, pct * 100).toFixed(0)}%"></i></span>` +
      `Context ${(used / 1000).toFixed(1)}k / ${Math.round(c.limit / 1024)}k · ${c.remembered_docs} docs`;
    chip.title = `${Math.round(pct * 100)}% of the usable context (${Math.round((c.budget || c.limit) / 1000)}k; the rest is reserved for the answer).\n` +
      `${c.compacted_results} old tool results compacted, ${c.dropped_turns} old turns trimmed.\n` +
      "Every document used in this chat stays remembered, so follow-up questions keep working.";

    const banner = $("ctxBanner");
    if (level !== "full" || dismissedBanner.has(currentId)) { banner.hidden = true; return; }
    banner.hidden = false;
    if (c.dropped_turns) {
      $("ctxBannerTitle").textContent = "Context is full";
      $("ctxBannerMsg").textContent = `The model can no longer see the ${c.dropped_turns} oldest exchange${c.dropped_turns > 1 ? "s" : ""} word for word (the documents they used are still remembered). Summarize into a note and continue fresh, or save the chat.`;
    } else {
      $("ctxBannerTitle").textContent = "Context almost full";
      $("ctxBannerMsg").textContent = c.compacted_results
        ? `Older search results are being compacted to fit (${c.compacted_results} so far). Consider continuing in a fresh chat with a summary note.`
        : `${Math.round(pct * 100)}% of the usable context is used. Soon older results will be compacted.`;
    }
  }

  // ---------- token counter under the textbox ----------
  const estTokens = (text) => Math.ceil(text.length / 4);   // ~4 chars per token for English prose
  function updateInputTokens() {
    const n = estTokens(input.value.trim());
    const out = $("inputTokens");
    out.textContent = `~${n.toLocaleString()} token${n === 1 ? "" : "s"}`;
    const { level } = ctxLevel(lastContext, n);
    out.classList.toggle("warn", level === "warn");
    out.classList.toggle("full", level === "full");
    out.title = level === "full" ? "This message plus the conversation fills the context: older content will be compacted or trimmed."
              : level === "warn" ? "The conversation is getting close to the context limit."
              : "Estimated tokens in your message";
  }

  // ---------- answer stats ----------
  function addStats(turn, st) {
    if (!st) return;
    turn.querySelector(".stats")?.remove();
    const parts = [st.model, `${(st.output_tokens || 0).toLocaleString()} tokens out`];
    if (st.tokens_per_s) parts.push(`${st.tokens_per_s} tok/s`);
    parts.push(`${st.seconds}s`);
    if (st.context_tokens) parts.push(`${(st.context_tokens / 1000).toFixed(1)}k context`);
    if (st.model_calls > 1) parts.push(`${st.model_calls} model calls`);
    turn.append(el("div", "stats", parts.filter(Boolean).map((p) => `<span>${esc(String(p))}</span>`).join("")));
  }

  // ---------- rendering ----------
  const emptyEl = $("empty"), emptyAdd = $("emptyAdd");   // detached from the DOM while a chat is open
  function scrollBottom(force) {
    const near = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 140;
    if (force || near) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addUser(text) {
    emptyEl.remove();
    const m = el("div", "msg user");
    const b = el("div", "bubble"); b.textContent = text;
    m.append(b); messagesEl.append(m);
    return m;
  }

  function addTurn() {
    const m = el("div", "msg assistant");
    messagesEl.append(m);
    return m;
  }

  function fmtArgs(args) {
    return Object.entries(args || {}).map(([k, v]) => k === "query" || k === "question" ? `"${v}"` : `${k}=${v}`).join(" · ");
  }

  function addStep(turn, call) {
    let steps = turn.lastElementChild;
    if (!steps || !steps.classList.contains("steps")) { steps = el("div", "steps"); turn.append(steps); }
    const d = el("details", "step pending");
    d.open = !!prefs.expandSteps;
    d.dataset.name = call.name;
    d.innerHTML = `<summary><span class="spin"></span><span class="name">${esc(call.name)}</span>` +
      `<span class="args">${esc(fmtArgs(call.arguments))}</span><span class="meta"></span></summary><div class="body"></div>`;
    steps.append(d);
    return d;
  }

  function fillStep(step, name, result) {
    step.classList.remove("pending");
    step.querySelector(".spin")?.remove();
    const meta = step.querySelector(".meta"), body = step.querySelector(".body");
    let html;
    if (result && result.error) {
      meta.textContent = "error"; html = `<p class="error">${esc(result.error)}</p>`;
    } else if (name === "search_documents") {
      const rows = Array.isArray(result) ? result : Array.isArray(result?.results) ? result.results : [];
      meta.textContent = `${rows.length} passage${rows.length === 1 ? "" : "s"}`;
      html = rows.length ? rows.map((r) => `<div class="passage"><div class="p-head">${window.docLink(r.doc_id, r.page)} ` +
          `<b>${esc(r.title || "")}</b>${r.section ? ` · ${esc(r.section)}` : ""}</div>` +
          `<div class="p-text">${esc((r.text || "").slice(0, 500))}</div></div>`).join("")
        : `<p>${esc(result?.hint || "No matches")}</p>`;
      if (result?.note) html = `<p class="small">${esc(result.note)}</p>` + html;
    } else if (name === "list_documents") {
      const rows = Array.isArray(result) ? result : [];
      meta.textContent = `${rows.length} document${rows.length === 1 ? "" : "s"}`;
      html = rows.length ? `<div class="table-wrap"><table><thead><tr><th>Ref</th><th>Title</th><th>Type</th><th>Pages</th><th>Collection</th></tr></thead><tbody>` +
        rows.map((r) => `<tr><td>${window.docLink(r.id)}</td><td>${esc(r.title || "")}</td><td>${esc(r.kind || "")}</td>` +
          `<td>${r.pages ?? ""}</td><td>${esc(r.collection || "")}</td></tr>`).join("") + "</tbody></table></div>"
        : `<p>${esc(result?.hint || "No documents")}</p>`;
    } else if ((name === "document_info" || name === "read_document" || name === "scan_document") && result?.ref) {
      const id = result.ref.slice(1);
      if (name === "read_document") {
        const ch = result.chunks || [];
        meta.textContent = ch.length ? `chunks ${ch[0].chunk}–${ch[ch.length - 1].chunk} of ${result.total_chunks}` : "nothing to read";
        html = `<p>${window.docLink(id)} <b>${esc(result.title || "")}</b></p>` +
          ch.map((c) => `<div class="passage"><div class="p-head">${c.page ? `p.${c.page}` : ""}${c.section ? ` · ${esc(c.section)}` : ""}</div>` +
            `<div class="p-text">${esc(c.text.slice(0, 600))}${c.text.length > 600 ? "…" : ""}</div></div>`).join("");
      } else if (name === "scan_document") {
        const f = Array.isArray(result.findings) ? result.findings : [];
        meta.textContent = `${result.parts_read} part${result.parts_read === 1 ? "" : "s"} read · ${f.length} with findings`;
        html = `<p>${window.docLink(id)} <b>${esc(result.title || "")}</b>${result.sampled ? " (long document: evenly sampled parts)" : ""}</p>` +
          (f.length ? f.map((x) => `<div class="passage"><div class="p-head">${x.pages ? `pages ${esc(x.pages)}` : ""}</div><div class="p-text">${md(x.notes)}</div></div>`).join("")
            : `<p>${esc(String(result.findings))}</p>`);
      } else {
        meta.textContent = `${result.kind || ""} · ${result.pages ?? "?"} pages`;
        html = `<p>${window.docLink(id)} <b>${esc(result.title || "")}</b></p>` + (result.summary ? `<div class="content">${md(result.summary)}</div>` : "");
      }
    } else {
      html = `<pre><code>${esc(JSON.stringify(result, null, 2))}</code></pre>`;
    }
    body.innerHTML = html;
  }


  function renderHistory(msgs) {
    if (!msgs.length) { messagesEl.append(emptyEl); return; }
    emptyEl.remove();
    let turn = null;
    const pending = [];
    for (const m of msgs) {
      if (m.role === "user") { addUser(m.content); turn = null; continue; }
      if (!turn) turn = addTurn();
      if (m.role === "assistant") {
        if (m.content && m.content.trim()) turn.append(el("div", "content", md(m.content)));
        for (const tc of m.tool_calls || []) pending.push(addStep(turn, tc.function));
        if (m.stats) addStats(turn, m.stats);
      } else if (m.role === "tool") {
        const step = pending.shift();
        if (step) { let r; try { r = JSON.parse(m.content); } catch { r = m.content; } fillStep(step, m.tool_name, r); }
      }
    }
  }

  // ---------- sending ----------
  async function send(text) {
    text = text.trim();
    if (!text || generating) return;
    if (!currentId) {
      const c = await api("/api/chats", { method: "POST" });
      currentId = c.id;
      history.replaceState(null, "", "#" + c.id);
    }
    input.value = ""; autosize(); updateInputTokens();
    addUser(text);
    const turn = addTurn();
    setGenerating(true);
    scrollBottom(true);

    let contentEl = null, raw = "", thinkEl = null, frame = 0;
    const pending = [];
    const paint = () => { frame = 0; if (contentEl) { contentEl.innerHTML = md(raw); scrollBottom(); } };

    const handle = ({ type, data }) => {
      switch (type) {
        case "turn_start":
          raw = ""; contentEl = el("div", "content cursor"); turn.append(contentEl); thinkEl = null; break;
        case "thinking":
          if (!thinkEl) { thinkEl = el("div", "thinking"); turn.insertBefore(thinkEl, contentEl); }
          thinkEl.textContent += data; break;
        case "token":
          raw += data; if (!frame) frame = requestAnimationFrame(paint); break;
        case "turn_end":
          if (contentEl) { contentEl.classList.remove("cursor"); if (!raw.trim()) contentEl.remove(); else contentEl.innerHTML = md(raw); }
          contentEl = null; break;
        case "tool_call":
          pending.push(addStep(turn, data)); scrollBottom(); break;
        case "tool_result": {
          const step = pending.shift(); if (step) fillStep(step, data.name, data.result); break;
        }
        case "context": showContext(data); break;
        case "stats": addStats(turn, data); scrollBottom(); break;
        case "progress": {
          const step = pending[0]; if (step) step.querySelector(".meta").textContent = data; break;
        }
        case "warning": turn.append(el("p", "small", esc(data))); break;
        case "error": turn.append(el("p", "error", esc(data))); break;
      }
    };

    try {
      const r = await fetch(`/api/chats/${currentId}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: text }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          if (chunk.startsWith("data: ")) handle(JSON.parse(chunk.slice(6)));
        }
      }
    } catch (e) {
      turn.append(el("p", "error", esc(String(e.message || e))));
    } finally {
      $("chatActions").hidden = false;
      if (contentEl) contentEl.classList.remove("cursor");
      turn.querySelectorAll(".step.pending .spin").forEach((s) => s.remove());
      setGenerating(false);
      loadChats().then(() => { const c = chats.find((c) => c.id === currentId); if (c) $("chatTitle").textContent = c.title; });
    }
  }

  function setGenerating(on) {
    generating = on;
    $("send").hidden = on; $("stop").hidden = !on;
  }

  // ---------- document drawer ----------
  const fmtSize = (n) => n == null ? "" : n >= 2 ** 20 ? (n / 2 ** 20).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";

  async function openDoc(id, page) {
    const drawer = $("drawer"), body = $("drawerBody");
    $("drawerTitle").textContent = "D" + id;
    body.innerHTML = '<p class="small">Loading…</p>';
    drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false");
    try {
      const d = await api(`/api/documents/${id}`);
      const meta = Object.entries(d.meta || {}).filter(([k]) => !["page_count", "slide_count", "sheets"].includes(k))
        .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join("");
      body.innerHTML = `
        <h3 style="margin-top:0">${esc(d.title)}</h3>
        <div class="drawer-actions">
          <button class="primary" id="docAsk">Ask about this document</button>
          <a class="btn" href="/api/documents/${d.id}/file" target="_blank" rel="noopener">Open original ↗</a>
          <button id="docSummarize">${d.summary ? "↻ Re-summarize" : "✎ Summarize"}</button>
        </div>
        ${d.warning ? `<p class="error">⚠ ${esc(d.warning)}</p>` : ""}${d.error ? `<p class="error">${esc(d.error)}</p>` : ""}
        <dl class="kv">
          <dt>File</dt><dd>${esc(d.file)}</dd>
          <dt>Type</dt><dd>${esc(d.kind || "")} · ${fmtSize(d.size)}</dd>
          <dt>Pages</dt><dd>${d.pages ?? "—"} · ${d.chunks} chunks · ${(d.chars || 0).toLocaleString()} chars</dd>
          <dt>Collection</dt><dd>${esc(d.collection || "—")}</dd>
          <dt>Added</dt><dd>${new Date(d.added * 1000).toLocaleString()}</dd>
          <dt>Status</dt><dd>${esc(d.status)}</dd>
          ${meta}
        </dl>
        ${d.summary ? `<div class="section-title">Summary</div><div class="content">${md(d.summary)}</div>` : ""}
        <div id="docPassage"></div>
        ${d.outline?.length ? `<div class="section-title">Outline</div><ol class="refs outline">${d.outline.map((o) =>
          `<li><a href="#" data-page="${o.page ?? ""}" data-chunk="${o.chunk}">${esc(o.section)}</a>${d.pages > 1 ? ` <span class="small">p.${o.page}</span>` : ""}</li>`).join("")}</ol>` : ""}`;
      $("docAsk").onclick = () => { closeDrawer(); input.value = `Using D${d.id} (${d.title}): `; autosize(); input.focus(); };
      $("docSummarize").onclick = async () => {
        await api(`/api/documents/${d.id}/summarize`, { method: "POST" });
        $("docSummarize").disabled = true; $("docSummarize").textContent = "Summarizing in background…";
      };
      body.querySelectorAll(".outline a").forEach((a) => a.onclick = (e) => {
        e.preventDefault(); showPassage(d, a.dataset.page && d.pages > 1 ? Number(a.dataset.page) : null, Number(a.dataset.chunk));
      });
      if (page || d.chunks) showPassage(d, page && d.pages > 1 ? page : null, 0, !page);
    } catch (e) {
      body.innerHTML = `<p class="error">${esc(String(e.message || e))}</p>`;
    }
  }

  async function showPassage(d, page, chunk, collapsed) {
    const box = $("docPassage");
    if (!box) return;
    const q = page ? `page=${page}&count=4` : `chunk=${chunk || 0}&count=4`;
    try {
      const r = await api(`/api/documents/${d.id}/read?${q}`);
      const where = page ? `Page ${page}` : r.chunks.length ? `From chunk ${r.chunks[0].chunk}` : "";
      box.innerHTML = `<details class="passage-box" ${collapsed ? "" : "open"}><summary class="section-title">Text · ${esc(where)}</summary>` +
        r.chunks.map((c) => `<div class="passage"><div class="p-head">${c.page && d.pages > 1 ? `p.${c.page}` : ""}${c.section ? ` · ${esc(c.section)}` : ""}</div>` +
          `<div class="p-text">${md(c.text)}</div></div>`).join("") +
        (r.next_chunk != null ? `<button class="link-btn" id="moreText">Continue reading</button>` : "") + "</details>";
      if (!collapsed) box.scrollIntoView({ block: "start", behavior: "smooth" });
      if ($("moreText")) $("moreText").onclick = () => showPassage(d, null, r.next_chunk);
    } catch (e) {
      box.innerHTML = `<p class="error">${esc(e.message)}</p>`;
    }
  }
  const closeDrawer = () => { $("drawer").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); };

  // ---------- status ----------
  async function refreshStatus() {
    try {
      const s = await api("/api/status");
      $("ollamaDot").className = "dot " + (s.ollama ? "ok" : "bad");
      $("modelName").textContent = s.ollama ? s.chat_model : "Ollama not reachable";
      $("appVersion").textContent = s.version;
      $("modelChip").textContent = s.chat_model;
      // placement: how much of each loaded model sits in VRAM (100% = no CPU offload); not utilization
      const placement = s.loaded.map((m) => {
        const pct = m.size ? Math.round(100 * m.vram / m.size) : 0;
        return `${m.model.split(":")[0]} ${pct >= 100 ? "in VRAM" : `${pct}% VRAM / ${100 - pct}% CPU`}`;
      }).join(" · ") || "no model loaded (loads on first message)";
      const g = s.gpu;
      $("gpuInfo").textContent = (g ? `GPU load ${Math.round(g.util)}% · ${(g.mem_used_mb / 1024).toFixed(1)}/${(g.mem_total_mb / 1024).toFixed(0)} GB\n` : "") + placement;
      $("gpuInfo").title = "GPU load = live utilization from nvidia-smi.\n'in VRAM' = the whole model is loaded on the GPU (fastest); " +
        "a CPU share means it did not fit and runs slower.\nModels stay loaded 30 min after the last request.";
      const L = s.library;
      const pct = L.chunks ? Math.min(100, 100 * s.vectors_loaded / L.chunks) : 0;
      $("indexBar").style.width = pct.toFixed(1) + "%";
      $("indexText").textContent = `${L.ready} docs · ${s.vectors_loaded.toLocaleString()} chunks`;
      $("indexText").title = `${L.ready} documents ready, ${L.chunks.toLocaleString()} chunks, ${s.vectors_indexed.toLocaleString()} embedded, ${s.vectors_loaded.toLocaleString()} loaded for search`;
      $("libCount").textContent = L.documents ? L.documents : "";
      $("jobText").hidden = !s.jobs;
      $("jobText").textContent = s.jobs ? `⏳ ${s.jobs} indexing / summary job${s.jobs === 1 ? "" : "s"} running` : "";
      emptyAdd.hidden = L.documents > 0;
      if (s.jobs || libJobs) { libJobs = s.jobs; if (onLibrary()) loadLibrary(); }
      $("reloadIndex").hidden = s.vectors_indexed <= s.vectors_loaded;
    } catch {
      $("ollamaDot").className = "dot bad"; $("modelName").textContent = "server offline";
    }
  }

  // ---------- wiring ----------
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; }
  input.addEventListener("input", () => { autosize(); updateInputTokens(); });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    const sendIt = prefs.enterSends ? !e.shiftKey && !e.ctrlKey : (e.ctrlKey || e.metaKey);
    if (sendIt) { e.preventDefault(); send(input.value); }
  });
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  $("stop").onclick = () => currentId && api(`/api/chats/${currentId}/stop`, { method: "POST" });
  $("newChat").onclick = () => { if (!generating) newChat(); };
  const narrow = () => matchMedia("(max-width: 900px)").matches;
  $("toggleSidebar").onclick = () => $("sidebar").classList.toggle(narrow() ? "shown" : "hidden");
  const closeSidebar = () => $("sidebar").classList[narrow() ? "remove" : "add"](narrow() ? "shown" : "hidden");
  $("closeSidebar").onclick = closeSidebar;
  $("sidebarBackdrop").onclick = closeSidebar;   // tap outside the overlay menu on narrow screens
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && narrow() && $("sidebar").classList.contains("shown")) closeSidebar();
  });
  $("sidebar").addEventListener("click", (e) => {   // on phones, close the overlay after picking a chat
    if (narrow() && e.target.closest(".chat-item a, .new-chat")) $("sidebar").classList.remove("shown");
  });
  $("closeDrawer").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a.doc");
    if (a) { e.preventDefault(); openDoc(a.dataset.id, a.dataset.page ? Number(a.dataset.page) : null); }
  });
  // ---------- starter questions (chapters) ----------
  async function loadStarters() {
    const tabs = $("topicTabs"), list = $("suggestions");
    if (!tabs || !list) return;
    let chapters;
    try { chapters = (await (await fetch("/static/questions.json")).json()).chapters || []; } catch { return; }
    if (!chapters.length) return;
    const showChapter = (ch, btn) => {
      tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
      list.innerHTML = "";
      for (const q of ch.questions || []) {
        const qb = el("button", null, escapeHtml(q));
        qb.onclick = () => send(q);
        list.append(qb);
      }
    };
    tabs.innerHTML = "";
    chapters.forEach((ch, i) => {
      const b = el("button", null, escapeHtml(ch.title));
      b.onclick = () => showChapter(ch, b);
      tabs.append(b);
      if (i === 0) showChapter(ch, b);
    });
  }
  loadStarters();
  $("reloadIndex").onclick = async () => {
    $("reloadIndex").textContent = "Loading…";
    await api("/api/reload-index", { method: "POST" }).catch(() => {});
    $("reloadIndex").textContent = "Load newly indexed vectors";
    refreshStatus();
  };
  // ---------- save / summarize / reset ----------
  $("exportChat").onclick = () => currentId && download(`/api/chats/${currentId}/export`);
  $("bannerSave").onclick = () => currentId && download(`/api/chats/${currentId}/export`);
  $("summarizeChat").onclick = async () => {
    if (!currentId || generating) return;
    const note = await summarizeCurrent([$("summarizeChat")]);
    if (note) openNote(note.id);
  };
  $("bannerContinue").onclick = async () => {
    if (!currentId || generating) return;
    const note = await summarizeCurrent([$("bannerContinue"), $("summarizeChat")]);
    if (note) startChatWithNotes([note.id]);
  };
  $("bannerReset").onclick = () => {
    if (generating) return;
    if (confirm("Start a new empty chat? This chat stays saved in the sidebar.")) newChat();
  };
  $("bannerDismiss").onclick = () => { dismissedBanner.add(currentId); $("ctxBanner").hidden = true; };

  // ---------- settings dialog ----------
  const dlg = $("settingsPage");   // settings page (was a dialog)
  const onSettings = () => !dlg.hidden;
  function showSettingsPage() {
    hideLibraryPage();
    $("chatView").hidden = true; dlg.hidden = false;
    $("openSettings").classList.add("active");
    document.title = "Settings · Metasploit API Explorer";
    syncPrefControls();
    api("/api/settings").then(fillServerSettings).catch((e) => { $("savedNote").textContent = "⚠ " + e.message; });
    loadModels();
    loadKeys();
  }
  function hideSettingsPage() {
    if (dlg.hidden) return;
    dlg.hidden = true; $("chatView").hidden = false;
    $("openSettings").classList.remove("active");
    document.title = "Metasploit API Explorer";
  }
  let savedTimer = 0;
  function syncPrefControls() {
    dlg.querySelectorAll(".seg[data-pref]").forEach((seg) =>
      seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === prefs[seg.dataset.pref])));
    dlg.querySelectorAll("input[data-pref]").forEach((i) => { i.checked = !!prefs[i.dataset.pref]; });
  }
  function fillServerSettings({ values, options, model_info }) {
    const mi = model_info;
    $("modelInfo").textContent = mi
      ? [mi.params, mi.quant, `${mi.size_gb} GB`, mi.context_length && `max context ${Math.round(mi.context_length / 1024)}k`,
         mi.capabilities.filter((c) => c !== "completion").join(", "),
         mi.loaded ? (mi.vram_pct >= 100 ? "loaded in VRAM" : `loaded ${mi.vram_pct}% VRAM`) : "not loaded"].filter(Boolean).join(" · ")
      : "";
    $("setThink").disabled = !!mi && !mi.capabilities.includes("thinking");
    $("setThink").title = $("setThink").disabled ? "This model has no reasoning mode" : "";
    $("modelChip").textContent = values.chat_model;
    $("setModel").innerHTML = options.chat_model.map((m) => `<option ${m === values.chat_model ? "selected" : ""}>${esc(m)}</option>`).join("");
    $("setCtx").innerHTML = options.num_ctx.map((n) => `<option value="${n}" ${n === values.num_ctx ? "selected" : ""}>${Math.round(n / 1024)}k tokens</option>`).join("");
    $("setTemp").value = values.temperature; $("tempVal").textContent = Number(values.temperature).toFixed(2);
    $("setThink").checked = values.think;
    $("setRounds").value = values.max_tool_rounds;
    $("setApi").checked = !!values.api_enabled;
    $("setSummary").checked = !!values.auto_summary;
    $("setLength").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === values.answer_length));
  }
  async function saveSetting(name, value) {
    try {
      fillServerSettings(await api("/api/settings", { method: "PUT", body: JSON.stringify({ [name]: value }) }));
      $("savedNote").textContent = "Saved ✓";
      refreshStatus();
      if (name === "num_ctx" && currentId) api(`/api/chats/${currentId}`).then((c) => showContext(c.context)).catch(() => {});
    } catch (e) {
      $("savedNote").textContent = "⚠ " + e.message;
    }
    clearTimeout(savedTimer); savedTimer = setTimeout(() => { $("savedNote").textContent = ""; }, 2500);
  }
  function openSettings() {
    if (location.hash === "#settings") showSettingsPage();
    else location.hash = "settings";   // hashchange -> showSettingsPage; browser Back returns to the chat
  }
  $("openSettings").onclick = openSettings;
  $("modelChip").onclick = () => { openSettings(); setTimeout(() => $("setModel").scrollIntoView({ block: "center" }), 80); };

  // ---------- model management ----------
  async function loadModels() {
    let d;
    try { d = await api("/api/models"); } catch (e) { $("modelList").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
    $("modelSuggestions").innerHTML = d.suggested.map((n) => `<option value="${esc(n)}">`).join("");
    $("modelList").innerHTML = "";
    for (const m of d.installed) {
      const row = el("div", "model-row" + (m.name === d.current ? " current" : ""));
      const badges = m.embedding ? '<span class="badge">embeddings</span>'
        : m.tools ? '<span class="badge ok">tools</span>' : '<span class="badge no">no tools</span>';
      const inUse = m.name === d.current || m.name === d.embed_model || m.name === d.embed_model + ":latest";
      row.innerHTML = `<div class="grow"><span class="mname">${esc(m.name)}</span>${badges}` +
        `${m.name === d.current ? '<span class="badge cur">in use</span>' : ""}` +
        `<div class="mmeta">${[m.params, m.quant, `${m.size_gb} GB`, m.context_length && `ctx ${Math.round(m.context_length / 1024)}k`].filter(Boolean).join(" · ")}</div></div>`;
      if (m.tools && m.name !== d.current) {
        const use = el("button"); use.textContent = "Use";
        use.onclick = () => saveSetting("chat_model", m.name).then(loadModels);
        row.append(use);
      }
      if (!inUse) {
        const del = el("button", "danger"); del.textContent = "Delete"; del.title = "Remove from disk";
        del.onclick = async () => {
          if (!confirm(`Delete ${m.name} from disk (${m.size_gb} GB)? You can download it again later.`)) return;
          try { await api(`/api/models/${encodeURIComponent(m.name)}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
          loadModels();
        };
        row.append(del);
      }
      $("modelList").append(row);
    }
  }

  let pullAbort = null;
  const fmtGB = (b) => (b / 2 ** 30).toFixed(b >= 2 ** 30 ? 1 : 2) + " GB";
  async function pullModel(name) {
    name = name.trim();
    if (!name || pullAbort) return;
    const box = $("pullBox");
    box.hidden = false; box.classList.remove("error");
    $("pullStatus").textContent = `Checking ${name}…`; $("pullBytes").textContent = ""; $("pullBar").style.width = "0%";
    $("pullBtn").disabled = true; $("pullCancel").hidden = false;
    pullAbort = new AbortController();
    let installed = null, failed = null;
    try {
      const r = await fetch("/api/models/pull", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }), signal: pullAbort.signal });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
          if (!chunk.startsWith("data: ")) continue;
          const { type, data } = JSON.parse(chunk.slice(6));
          if (type === "progress") {
            $("pullStatus").textContent = data.status;
            if (data.total) {
              $("pullBar").style.width = (100 * data.completed / data.total).toFixed(1) + "%";
              $("pullBytes").textContent = `${fmtGB(data.completed)} / ${fmtGB(data.total)}`;
            }
          } else if (type === "installed") installed = data;
          else if (type === "error") failed = data;
          else if (type === "cancelled") failed = "Download cancelled";
        }
      }
    } catch (e) {
      failed = e.name === "AbortError" ? "Download cancelled (partial data is kept; downloading again resumes)" : e.message;
    } finally {
      pullAbort = null; $("pullBtn").disabled = false; $("pullCancel").hidden = true;
    }
    if (failed) { box.classList.add("error"); $("pullStatus").textContent = failed; $("pullBytes").textContent = ""; return; }
    if (installed && !installed.tools) {
      box.classList.add("error");
      $("pullStatus").textContent = `${installed.name} installed, but it has no tool calling, so the agent cannot use it.`;
      loadModels(); return;
    }
    $("pullStatus").textContent = `${installed.name} ready, switching…`; $("pullBar").style.width = "100%";
    await saveSetting("chat_model", installed.name);
    $("pullStatus").textContent = `Now using ${installed.name}. It loads onto the GPU with your next message.`;
    $("pullName").value = "";
    loadModels();
  }
  $("pullBtn").onclick = () => pullModel($("pullName").value);
  $("pullName").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); pullModel($("pullName").value); } });
  $("pullCancel").onclick = () => pullAbort?.abort();
  const closeSettings = () => { location.hash = currentId || ""; if (!currentId) { history.replaceState(null, "", location.pathname); hideSettingsPage(); hideLibraryPage(); } };
  $("closeSettings").onclick = closeSettings;
  $("toggleSidebar2").onclick = () => $("toggleSidebar").click();
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && onSettings() && !$("drawer").classList.contains("open")) closeSettings();
    if (e.key === "Escape" && onLibrary() && !$("drawer").classList.contains("open")) closeSettings();
  });
  dlg.querySelectorAll(".seg[data-pref] button").forEach((b) => b.onclick = () => {
    prefs[b.parentElement.dataset.pref] = b.dataset.v; savePrefs(); applyPrefs(); syncPrefControls();
  });
  dlg.querySelectorAll("input[data-pref]").forEach((i) => i.onchange = () => {
    prefs[i.dataset.pref] = i.checked; savePrefs(); applyPrefs();
  });
  $("setModel").onchange = (e) => saveSetting("chat_model", e.target.value).then(loadModels);
  $("setCtx").onchange = (e) => saveSetting("num_ctx", Number(e.target.value));
  $("setThink").onchange = (e) => saveSetting("think", e.target.checked);
  $("setSummary").onchange = (e) => saveSetting("auto_summary", e.target.checked);
  $("setRounds").onchange = (e) => saveSetting("max_tool_rounds", Number(e.target.value));
  $("setTemp").oninput = (e) => { $("tempVal").textContent = Number(e.target.value).toFixed(2); };
  $("setTemp").onchange = (e) => saveSetting("temperature", Number(e.target.value));

  // ---------- API access ----------
  const apiBase = () => `${location.origin}/api/v1`;
  let exKey = "YOUR_API_KEY", exLang = "curl";
  function renderExample() {
    const b = apiBase(), k = exKey;
    const ex = {
      curl: `# search the API reference (fast, no LLM)
curl -H "Authorization: Bearer ${k}" "${b}/search?q=send_request_cgi+HttpClient&limit=5"

# ask the agent (save:true keeps the conversation; reuse chat_id for follow-ups)
curl -X POST ${b}/ask -H "Authorization: Bearer ${k}" -H "Content-Type: application/json" \\
  -d '{"question": "Write a minimal Msf::Auxiliary HTTP scanner that reports a vuln", "save": true}'`,
      python: `import requests

API = "${b}"
H = {"Authorization": "Bearer ${k}"}

hits = requests.get(f"{API}/search", headers=H, params={"q": "register_options datastore", "limit": 5}).json()
for h in hits["results"]:
    print(h["ref"], h["title"], h["text"][:80])

r = requests.post(f"{API}/ask", headers=H, timeout=600, json={
    "question": "How do I use send_request_cgi? Show a short example", "save": True}).json()
print(r["answer"])

# follow-up in the same conversation
r = requests.post(f"{API}/ask", headers=H, timeout=600, json={
    "question": "Now add a check() method using that", "chat_id": r["chat_id"]}).json()
print(r["answer"])`,
      ps: `$H = @{ Authorization = "Bearer ${k}" }
$API = "${b}"

(Invoke-RestMethod "$API/search?q=send_request_cgi&limit=5" -Headers $H).results | Format-Table ref, title

$body = @{ question = "Which mixins does a remote HTTP exploit include?"; save = $true } | ConvertTo-Json
(Invoke-RestMethod "$API/ask" -Method Post -Headers $H -ContentType "application/json" -Body $body -TimeoutSec 600).answer`,
    };
    $("exCode").textContent = ex[exLang];
  }

  async function loadKeys() {
    $("apiBase").textContent = apiBase();
    let d;
    try { d = await api("/api/keys"); } catch (e) { $("keyList").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
    $("apiReach").innerHTML = d.local_only
      ? `Reachable only from this computer (server listens on ${esc(d.bind_host)}). For other machines, start it with <code>python -m msfrag web --host 0.0.0.0</code>.`
      : `<span class="reach-warn">Listening on ${esc(d.bind_host)}:${d.port} — other machines on your network can reach it. API calls need a key, but the web UI itself has no login.</span>`;
    $("keyList").innerHTML = d.keys.length ? "" : '<p class="note">No keys yet.</p>';
    for (const k of d.keys) {
      const row = el("div", "model-row");
      row.innerHTML = `<div class="grow"><span class="mname">${esc(k.name)}</span><span class="badge">${esc(k.prefix)}…</span>` +
        `<div class="mmeta">created ${new Date(k.created * 1000).toLocaleString()} · ` +
        `${k.last_used ? `last used ${ago(k.last_used) === "now" ? "just now" : ago(k.last_used) + " ago"} · ${k.uses} call${k.uses === 1 ? "" : "s"}` : "never used"}</div></div>`;
      const rev = el("button", "danger"); rev.textContent = "Revoke";
      rev.onclick = async () => {
        if (!confirm(`Revoke key "${k.name}"? Programs using it will stop working.`)) return;
        await api(`/api/keys/${k.id}`, { method: "DELETE" }).catch((e) => alert(e.message));
        loadKeys();
      };
      row.append(rev); $("keyList").append(row);
    }
    renderExample();
  }

  $("setLength").querySelectorAll("button").forEach((b) => b.onclick = () => saveSetting("answer_length", b.dataset.v));
  $("setApi").onchange = async (e) => {
    await saveSetting("api_enabled", e.target.checked);
    $("apiNote").textContent = e.target.checked ? "API on" : "API off";
    setTimeout(() => { $("apiNote").textContent = ""; }, 2500);
  };
  $("keyCreate").onclick = async () => {
    const name = $("keyName").value.trim();
    if (!name) { $("keyName").focus(); return; }
    try {
      const k = await api("/api/keys", { method: "POST", body: JSON.stringify({ name }) });
      $("keyValue").textContent = k.key; $("keyReveal").hidden = false; $("keyName").value = "";
      exKey = k.key; loadKeys();
    } catch (e) { alert(e.message); }
  };
  $("keyName").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("keyCreate").click(); } });
  const copy = async (text, btn) => {
    try { await navigator.clipboard.writeText(text); } catch {
      const t = el("textarea"); t.value = text; document.body.append(t); t.select(); document.execCommand("copy"); t.remove();
    }
    const old = btn.textContent; btn.textContent = "Copied ✓"; setTimeout(() => { btn.textContent = old; }, 1500);
  };
  $("keyCopy").onclick = () => copy($("keyValue").textContent, $("keyCopy"));
  $("exCopy").onclick = () => copy($("exCode").textContent, $("exCopy"));
  $("exTabs").querySelectorAll("button").forEach((b) => b.onclick = () => {
    exLang = b.dataset.ex;
    $("exTabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    renderExample();
  });

  window.addEventListener("hashchange", () => {
    const id = location.hash.slice(1);
    if (id === "settings") { showSettingsPage(); return; }
    if (id === "library") { showLibraryPage(); return; }
    hideSettingsPage(); hideLibraryPage();
    if (id && id !== currentId) openChat(id);
  });

  // ---------- library page ----------
  const libPage = $("libraryPage");
  const onLibrary = () => !libPage.hidden;
  let libJobs = 0, libTimer = 0;
  function showLibraryPage() {
    hideSettingsPage();
    $("chatView").hidden = true; libPage.hidden = false;
    $("openLibrary").classList.add("active");
    document.title = "Library · Metasploit API Explorer";
    loadLibrary();
  }
  function hideLibraryPage() {
    if (libPage.hidden) return;
    libPage.hidden = true; $("chatView").hidden = false;
    $("openLibrary").classList.remove("active");
    document.title = "Metasploit API Explorer";
  }
  const STATUS = { ready: "ok", queued: "", indexing: "", error: "no" };
  async function loadLibrary() {
    let d;
    const q = $("libSearch").value.trim(), col = $("libCollection").value;
    try { d = await api(`/api/documents?${new URLSearchParams({ ...(q && { q }), ...(col && { collection: col }) })}`); }
    catch (e) { $("docList").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
    $("supportedNote").textContent = "Supported: " + d.supported.join(" ");
    const cols = d.collections.filter((c) => c.collection);
    $("collectionList").innerHTML = cols.map((c) => `<option value="${esc(c.collection)}">`).join("");
    $("libCollection").innerHTML = '<option value="">All collections</option>' +
      cols.map((c) => `<option ${c.collection === col ? "selected" : ""} value="${esc(c.collection)}">${esc(c.collection)} (${c.documents})</option>`).join("");
    const ready = d.documents.filter((x) => x.status === "ready");
    $("libSummary").textContent = `${ready.length} ready · ${ready.reduce((n, x) => n + (x.pages || 0), 0).toLocaleString()} pages` +
      (d.jobs ? ` · ${d.jobs} job${d.jobs === 1 ? "" : "s"} running` : "");
    $("docList").innerHTML = d.documents.length ? "" : `<p class="note">${q || col ? "Nothing matches." : "No documents yet: drop some files above."}</p>`;
    for (const x of d.documents) {
      const row = el("div", "model-row doc-row");
      const busyDoc = x.status === "queued" || x.status === "indexing";
      row.innerHTML = `<div class="grow"><a class="mname doc-open" href="#" data-id="${x.id}">${esc(x.title)}</a>` +
        `<span class="badge">${esc(x.ref)}</span><span class="badge">${esc(x.kind || "")}</span>` +
        (x.collection ? `<span class="badge cur">${esc(x.collection)}</span>` : "") +
        (x.status !== "ready" ? `<span class="badge ${STATUS[x.status] ?? ""}">${busyDoc ? "⏳ " : ""}${esc(x.status)}</span>` : "") +
        (x.warning ? `<span class="badge no" title="${esc(x.warning)}">⚠ check</span>` : "") +
        `<div class="mmeta">${esc(x.file)} · ${[x.pages && `${x.pages} page${x.pages === 1 ? "" : "s"}`, x.chunks && `${x.chunks} chunks`, ago(x.added)].filter(Boolean).join(" · ")}` +
        `${x.error ? ` · <span class="reach-warn">${esc(x.error)}</span>` : ""}</div>` +
        (x.summary ? `<div class="mmeta doc-sum">${esc(x.summary.replace(/[*#]/g, "").slice(0, 220))}</div>` : "") + "</div>";
      if (!busyDoc) {
        const re = el("button"); re.textContent = "Re-index"; re.title = "Read the file again";
        re.onclick = async () => { await api(`/api/documents/${x.id}/reindex`, { method: "POST" }).catch((e) => alert(e.message)); loadLibrary(); };
        const del = el("button", "danger"); del.textContent = "Remove";
        del.title = "Remove from the library (files you indexed in place stay on disk)";
        del.onclick = async () => {
          if (!confirm(`Remove "${x.title}" from the library?`)) return;
          await api(`/api/documents/${x.id}`, { method: "DELETE" }).catch((e) => alert(e.message));
          loadLibrary(); refreshStatus();
        };
        row.append(re, del);
      }
      $("docList").append(row);
    }
    libJobs = d.jobs || d.documents.some((x) => x.status === "queued" || x.status === "indexing");
    clearTimeout(libTimer);
    if (libJobs && onLibrary()) libTimer = setTimeout(loadLibrary, 2500);
  }
  $("docList").addEventListener("click", (e) => {
    const a = e.target.closest("a.doc-open");
    if (a) { e.preventDefault(); openDoc(a.dataset.id); }
  });
  async function uploadFiles(files) {
    const collection = $("addCollection").value.trim();
    for (const f of files) {
      const line = el("div", "upload-line", `<span>${esc(f.name)}</span><span class="small">uploading…</span>`);
      $("uploadList").prepend(line);
      try {
        const qs = new URLSearchParams({ name: f.name, ...(collection && { collection }) });
        const r = await fetch(`/api/documents/upload?${qs}`, { method: "POST", body: f });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
        line.lastChild.textContent = "queued for indexing ✓";
      } catch (e) {
        line.lastChild.textContent = e.message; line.classList.add("error");
      }
      loadLibrary();
    }
    refreshStatus();
  }
  $("pickFiles").onclick = () => $("fileInput").click();
  $("fileInput").onchange = (e) => { uploadFiles([...e.target.files]); e.target.value = ""; };
  const drop = $("drop");
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
  $("addPathBtn").onclick = async () => {
    const path = $("addPath").value.trim();
    if (!path) { $("addPath").focus(); return; }
    try {
      const r = await api("/api/documents/add-path", { method: "POST", body: JSON.stringify({ path, collection: $("addCollection").value.trim() || null }) });
      $("addNote").textContent = `${r.queued} file${r.queued === 1 ? "" : "s"} queued ✓`;
      $("addPath").value = "";
      loadLibrary(); refreshStatus();
    } catch (e) { $("addNote").textContent = "⚠ " + e.message; }
    setTimeout(() => { $("addNote").textContent = ""; }, 4000);
  };
  $("addPath").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("addPathBtn").click(); } });
  let libSearchTimer = 0;
  $("libSearch").oninput = () => { clearTimeout(libSearchTimer); libSearchTimer = setTimeout(loadLibrary, 250); };
  $("libCollection").onchange = loadLibrary;
  const openLibrary = () => { if (location.hash === "#library") showLibraryPage(); else location.hash = "library"; };
  $("openLibrary").onclick = openLibrary;
  emptyAdd.onclick = openLibrary;
  $("closeLibrary").onclick = closeSettings;
  $("toggleSidebar3").onclick = () => $("toggleSidebar").click();

  applyPrefs();
  updateInputTokens();
  const initial = location.hash.slice(1);
  if (initial === "settings") { loadChats(); showSettingsPage(); }
  else if (initial === "library") { loadChats(); showLibraryPage(); }
  else if (initial) openChat(initial); else loadChats();
  loadNotes();
  $("newNote").onclick = (e) => {
    e.preventDefault();
    $("drawer").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false");
    noteEditor(null);
  };
  refreshStatus();
  setInterval(refreshStatus, 10000);
})();
