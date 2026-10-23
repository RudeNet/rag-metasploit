"""Local web chat UI: FastAPI + server-sent events over the tool-calling agent."""
import asyncio
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__, config, models, notes, settings
from ..agent import Agent
from ..extract import SUPPORTED, supported
from ..ingest import Worker, remove, stored_upload_path, walk
from ..library import Filters
from ..llm import client
from ..retriever import Retriever

STATIC = Path(__file__).parent / "static"
CHATS_DB = config.CHATS_DB

retriever: Retriever | None = None
worker: Worker | None = None
agents: dict[str, Agent] = {}
busy: set[str] = set()
tool_lock = threading.Lock()   # one SQLite connection is shared by all chats


@asynccontextmanager
async def _lifespan(_app):
    global retriever, worker
    settings.load()
    retriever = Retriever()
    worker = Worker(on_indexed=lambda: retriever.reload_vectors())
    _db().close()
    yield


app = FastAPI(title="Metasploit API Explorer", version=__version__, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"   # always revalidate UI assets after edits
    return resp


# ---- chat persistence ----------------------------------------------------
def _db():
    con = sqlite3.connect(CHATS_DB)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT, "
                "created REAL, updated REAL, messages TEXT, notes TEXT DEFAULT '[]')")
    con.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, title TEXT, content TEXT, "
                "created REAL, source_chat TEXT)")
    return con


def _chat_notes(chat_row):
    """Notes attached to a chat, as [{id, title, content}] (deleted notes are skipped)."""
    ids = json.loads(chat_row["notes"] or "[]")
    if not ids:
        return []
    with _db() as con:
        rows = {r["id"]: dict(r) for r in con.execute(
            f"SELECT id, title, content FROM notes WHERE id IN ({','.join('?' * len(ids))})", ids)}
    return [rows[i] for i in ids if i in rows]


def _load_chat(chat_id):
    with _db() as con:
        r = con.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "chat not found")
    return r


def _save_chat(chat_id, messages):
    msgs = [m for m in messages if m.get("role") != "system"]
    first = next((m["content"] for m in msgs if m["role"] == "user"), "New chat")
    with _db() as con:
        con.execute("UPDATE chats SET messages = ?, updated = ?, "
                    "title = CASE WHEN title = 'New chat' THEN ? ELSE title END WHERE id = ?",
                    (json.dumps(msgs, default=str), time.time(), first[:80], chat_id))


def new_agent(**kw):
    with tool_lock:
        agent = Agent(retriever=retriever, **kw)
    agent.lock = tool_lock
    return agent


def _agent(chat_id):
    if chat_id not in agents:
        row = _load_chat(chat_id)
        agents[chat_id] = new_agent(history=json.loads(row["messages"] or "[]"), notes=_chat_notes(row))
    return agents[chat_id]


# ---- pages / info ----------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    with tool_lock:
        st = retriever.db.stats()
    try:
        loaded = [{"model": m.model, "vram": m.size_vram or 0, "size": m.size or 0}
                  for m in client().ps().models]
        ollama_ok = True
    except Exception:
        loaded, ollama_ok = [], False
    return {"version": __version__, "chat_model": config.CHAT_MODEL, "embed_model": config.EMBED_MODEL, "ollama": ollama_ok,
            "loaded": loaded, "gpu": _gpu_usage(), "library": st, "vectors_loaded": len(retriever.vectors),
            "vectors_indexed": st["vectors"], "jobs": worker.pending()}


def _gpu_usage():
    """Live utilization from nvidia-smi (None if unavailable)."""
    import shutil
    import subprocess
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        util, used, total = (float(x) for x in out.strip().splitlines()[0].split(","))
        return {"util": util, "mem_used_mb": used, "mem_total_mb": total}
    except Exception:
        return None


def _ctx_choices(model_info):
    """Context options limited to what the model supports."""
    max_ctx = (model_info or {}).get("context_length") or max(settings.CONTEXT_CHOICES)
    return [n for n in settings.CONTEXT_CHOICES if n <= max_ctx] or [min(settings.CONTEXT_CHOICES)]


@app.get("/api/settings")
def get_settings():
    try:
        inst = models.installed()
    except Exception:
        inst = []
    chat_models = [m["name"] for m in inst if m["tools"]]
    if config.CHAT_MODEL not in chat_models:
        chat_models.insert(0, config.CHAT_MODEL)
    current = next((m for m in inst if m["name"] == config.CHAT_MODEL), None)
    return {"values": settings.current(),
            "options": {"chat_model": chat_models, "num_ctx": _ctx_choices(current)},
            "model_info": current}


@app.put("/api/settings")
def put_settings(values: dict):
    old_model = config.CHAT_MODEL
    if "chat_model" in values:
        info = models.find(str(values["chat_model"]))
        if info is None:
            raise HTTPException(400, f"{values['chat_model']} is not installed - download it first")
        if not info["tools"]:
            raise HTTPException(400, f"{info['name']} does not support tool calling, which the agent needs")
        values["chat_model"] = info["name"]
        allowed = _ctx_choices(info)
        if int(values.get("num_ctx", config.NUM_CTX)) not in allowed:
            values["num_ctx"] = max(allowed)   # new model has a smaller window
    try:
        settings.apply(values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    settings.save()
    if config.CHAT_MODEL != old_model and not busy:
        models.unload(old_model)   # free VRAM so the new model loads fully on the GPU
    return get_settings()


# ---- models ----------------------------------------------------------------
@app.get("/api/models")
def list_models():
    try:
        inst = models.installed()
    except Exception as e:
        raise HTTPException(502, f"Ollama not reachable: {e}")
    names = {m["name"] for m in inst}
    return {"installed": inst, "current": config.CHAT_MODEL, "embed_model": config.EMBED_MODEL,
            "suggested": [s for s in models.SUGGESTED if s not in names], "pulling": sorted(pulling)}


pulling: set[str] = set()


class PullBody(BaseModel):
    name: str


def _sse(queue, on_close):
    async def events():
        try:
            while True:
                kind, data = await queue.get()
                yield f"data: {json.dumps({'type': kind, 'data': data}, default=str)}\n\n"
                if kind == "done":
                    break
        finally:
            on_close()
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/models/pull")
async def pull_model(body: PullBody):
    """Download a model (server-sent progress events). Already-installed models finish immediately."""
    name = body.name.strip()
    if not models.valid_name(name):
        raise HTTPException(400, "invalid model name (e.g. qwen3:8b, llama3.1:8b)")
    name = models.normalize(name)
    if name in pulling:
        raise HTTPException(409, f"{name} is already downloading")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()

    def emit(kind, data):
        loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

    def work():
        try:
            if models.find(name) is None:
                for p in models.pull(name):
                    if cancel.is_set():
                        emit("cancelled", None)
                        return
                    emit("progress", p)
            info = models.find(name)
            if info is None:
                emit("error", f"{name} did not install")
            else:
                emit("installed", info)
        except Exception as e:
            msg = str(e)
            if "not found" in msg or "file does not exist" in msg:
                msg = f"'{name}' was not found in the Ollama library - check the name at ollama.com/library"
            emit("error", msg)
        finally:
            pulling.discard(name)
            emit("done", None)

    pulling.add(name)
    threading.Thread(target=work, daemon=True).start()
    return _sse(queue, cancel.set)   # client closed the stream -> stop downloading


@app.delete("/api/models/{name:path}")
def delete_model(name: str):
    name = models.normalize(name)
    if name in (config.CHAT_MODEL, models.normalize(config.EMBED_MODEL)):
        raise HTTPException(400, f"{name} is in use - switch to another model first")
    if models.find(name) is None:
        raise HTTPException(404, f"{name} is not installed")
    models.delete(name)
    return {"ok": True}


@app.post("/api/reload-index")
def reload_index():
    return {"vectors_loaded": retriever.reload_vectors()}


# ---- documents -------------------------------------------------------------
@app.get("/api/documents")
def list_documents(q: str | None = None, collection: str | None = None, kind: str | None = None):
    with tool_lock:
        docs = retriever.db.list_documents(q, Filters(collection=collection, kind=kind), 2000, include_all=True)
        cols = retriever.db.collections()
    return {"documents": docs, "collections": cols, "supported": SUPPORTED, "jobs": worker.pending()}


def _doc(ref):
    with tool_lock:
        d = retriever.db.get_document(ref)
    if d is None:
        raise HTTPException(404, f"document {ref} not found")
    return d


@app.get("/api/documents/{ref}")
def get_document(ref: str):
    return _doc(ref)


@app.get("/api/documents/{ref}/read")
def read_document(ref: str, page: int | None = None, chunk: int = 0, count: int = Query(6, ge=1, le=20)):
    d = _doc(ref)
    with tool_lock:
        return {"ref": d["ref"], "title": d["title"], **retriever.db.read(d["id"], chunk, count, page)}


@app.get("/api/documents/{ref}/file")
def document_file(ref: str, download: bool = False):
    d = _doc(ref)
    p = Path(d["path"])
    if not p.exists():
        raise HTTPException(404, f"the file is no longer at {p}")
    return FileResponse(p, filename=p.name, content_disposition_type="attachment" if download else "inline")


@app.post("/api/documents/upload")
async def upload_document(request: Request, name: str = Query(..., min_length=1, max_length=255),
                          collection: str | None = None):
    """Raw file body (no multipart). The file is kept in library/ and indexed in the background."""
    if not supported(Path(name)):
        raise HTTPException(400, f"unsupported file type - supported: {', '.join(SUPPORTED)}")
    target = stored_upload_path(name)
    size = 0
    with open(target, "wb") as f:
        async for block in request.stream():
            size += len(block)
            if size > config.MAX_FILE_MB * 2**20:
                f.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, f"file is larger than {config.MAX_FILE_MB} MB")
            f.write(block)
    if not size:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "empty file")
    worker.add(target, (collection or "").strip() or None)
    return {"ok": True, "stored_as": target.name, "size": size}


class AddPath(BaseModel):
    path: str
    collection: str | None = None


@app.post("/api/documents/add-path")
def add_path(body: AddPath):
    """Index a file or folder already on this computer, in place (not copied)."""
    p = Path(body.path.strip().strip('"')).expanduser()
    if not p.exists():
        raise HTTPException(404, f"{p} does not exist")
    files = list(walk([p]))
    if not files:
        raise HTTPException(400, "no supported files there")
    for f in files:
        worker.add(f, (body.collection or "").strip() or None)
    return {"queued": len(files)}


@app.post("/api/documents/{ref}/summarize")
def summarize_document(ref: str):
    worker.summarize(_doc(ref)["id"])
    return {"queued": True}


@app.post("/api/documents/{ref}/reindex")
def reindex_document(ref: str):
    worker.add(_doc(ref)["path"], force=True)
    return {"queued": True}


class DocPatch(BaseModel):
    title: str | None = None
    collection: str | None = None


@app.put("/api/documents/{ref}")
def update_document(ref: str, body: DocPatch):
    d = _doc(ref)
    vals = {}
    if body.title is not None and body.title.strip():
        vals["title"] = body.title.strip()[:200]
    if body.collection is not None:
        vals["collection"] = body.collection.strip() or None
    if vals:
        with tool_lock:
            retriever.db.conn.execute(f"UPDATE documents SET {', '.join(f'{k} = :{k}' for k in vals)} WHERE id = :id",
                                      {**vals, "id": d["id"]})
            retriever.db.conn.commit()
    return _doc(ref)


@app.delete("/api/documents/{ref}")
def delete_document(ref: str):
    d = _doc(ref)
    if d["status"] in ("queued", "indexing"):
        raise HTTPException(409, "document is being indexed - try again when it is ready")
    with tool_lock:
        remove(d["id"])
    retriever.reload_vectors()
    return {"ok": True}


# ---- chats -----------------------------------------------------------------
def _preview(messages_json):
    """Last visible message of a chat, for the sidebar."""
    for m in reversed(json.loads(messages_json or "[]")):
        text = (m.get("content") or "").strip()
        if m.get("role") in ("assistant", "user") and text:
            text = " ".join(text.replace("#", "").replace("*", "").replace("`", "").replace("|", " ").split())
            return ("You: " if m["role"] == "user" else "") + text[:110]
    return ""


@app.get("/api/chats")
def list_chats():
    with _db() as con:
        rows = con.execute("SELECT id, title, updated, messages FROM chats ORDER BY updated DESC").fetchall()
    return [{"id": r["id"], "title": r["title"], "updated": r["updated"], "preview": _preview(r["messages"])}
            for r in rows]


class NewChat(BaseModel):
    notes: list[str] = []


@app.post("/api/chats")
def create_chat(body: NewChat | None = None):
    chat_id = uuid.uuid4().hex[:12]
    now = time.time()
    attached = (body.notes if body else [])[:3]   # cap: each note costs up to ~2.5k tokens of context
    with _db() as con:
        con.execute("INSERT INTO chats (id, title, created, updated, messages, notes) "
                    "VALUES (?, 'New chat', ?, ?, '[]', ?)", (chat_id, now, now, json.dumps(attached)))
    return {"id": chat_id, "title": "New chat", "updated": now}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str):
    r = _load_chat(chat_id)
    return {"id": r["id"], "title": r["title"], "messages": json.loads(r["messages"] or "[]"),
            "context": _agent(chat_id).context_info,
            "notes": [{"id": n["id"], "title": n["title"]} for n in _chat_notes(r)]}


def _md_download(text, title):
    name = re.sub(r"[^\w\-]+", "_", title).strip("_")[:60] or "chat"
    return Response(text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.md"'})


@app.get("/api/chats/{chat_id}/export")
def export_chat(chat_id: str):
    r = _load_chat(chat_id)
    return _md_download(notes.export_markdown(r["title"], json.loads(r["messages"] or "[]"), _chat_notes(r)),
                        r["title"])


@app.post("/api/chats/{chat_id}/summarize")
def summarize_chat(chat_id: str):
    """Write a note summarizing the chat (runs the LLM; takes ~20-60 s)."""
    r = _load_chat(chat_id)
    messages = json.loads(r["messages"] or "[]")
    if not any(m.get("role") == "assistant" for m in messages):
        raise HTTPException(400, "nothing to summarize yet")
    try:
        content = notes.summarize(r["title"], messages, _chat_notes(r))
    except Exception as e:
        raise HTTPException(502, f"summary failed: {e}")
    note = {"id": uuid.uuid4().hex[:12], "title": f"Notes: {r['title']}"[:90], "content": content,
            "created": time.time(), "source_chat": chat_id}
    with _db() as con:
        con.execute("INSERT INTO notes VALUES (:id, :title, :content, :created, :source_chat)", note)
    return note


# ---- notes -----------------------------------------------------------------
@app.get("/api/notes")
def list_notes():
    with _db() as con:
        rows = con.execute("SELECT id, title, created, source_chat, length(content) AS size "
                           "FROM notes ORDER BY created DESC").fetchall()
    return [dict(r) for r in rows]


def _load_note(note_id):
    with _db() as con:
        r = con.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "note not found")
    return dict(r)


@app.get("/api/notes/{note_id}")
def get_note(note_id: str):
    return _load_note(note_id)


@app.get("/api/notes/{note_id}/export")
def export_note(note_id: str):
    n = _load_note(note_id)
    return _md_download(f"# {n['title']}\n\n{n['content']}\n", n["title"])


def _drop_agents(chat_id=None):
    """Forget cached agents (all, or one chat) so they are rebuilt with current notes; skips running ones."""
    for cid in [chat_id] if chat_id else list(agents):
        if cid not in busy:
            agents.pop(cid, None)


class NoteBody(BaseModel):
    title: str | None = None
    content: str | None = None


def _clean_note(body: NoteBody, partial: bool):
    vals = {}
    if body.title is not None or not partial:
        title = (body.title or "").strip()
        if not title:
            raise HTTPException(400, "title is required")
        vals["title"] = title[:120]
    if body.content is not None or not partial:
        content = (body.content or "").strip()
        if not content:
            raise HTTPException(400, "content is required")
        if len(content) > notes.NOTE_MAX_CHARS * 2:
            raise HTTPException(400, f"note too long (max {notes.NOTE_MAX_CHARS * 2:,} characters)")
        vals["content"] = content
    return vals


@app.post("/api/notes")
def create_note(body: NoteBody):
    """A hand-written note (e.g. pasted reference material)."""
    note = {"id": uuid.uuid4().hex[:12], **_clean_note(body, partial=False), "created": time.time(),
            "source_chat": None}
    with _db() as con:
        con.execute("INSERT INTO notes VALUES (:id, :title, :content, :created, :source_chat)", note)
    return note


@app.put("/api/notes/{note_id}")
def update_note(note_id: str, body: NoteBody):
    _load_note(note_id)
    vals = _clean_note(body, partial=True)
    if vals:
        with _db() as con:
            con.execute(f"UPDATE notes SET {', '.join(f'{k} = :{k}' for k in vals)} WHERE id = :id",
                        {**vals, "id": note_id})
        _drop_agents()   # chats using this note get the new text on their next message
    return _load_note(note_id)


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str):
    with _db() as con:
        con.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        for r in con.execute("SELECT id, notes FROM chats WHERE notes LIKE ?", (f'%"{note_id}"%',)).fetchall():
            ids = [i for i in json.loads(r["notes"] or "[]") if i != note_id]
            con.execute("UPDATE chats SET notes = ? WHERE id = ?", (json.dumps(ids), r["id"]))
    _drop_agents()
    return {"ok": True}


class ChatNotes(BaseModel):
    notes: list[str]


@app.put("/api/chats/{chat_id}/notes")
def set_chat_notes(chat_id: str, body: ChatNotes):
    """Attach/detach notes on an existing chat (max 3)."""
    _load_chat(chat_id)
    if chat_id in busy:
        raise HTTPException(409, "chat is generating")
    ids = list(dict.fromkeys(body.notes))
    if len(ids) > 3:
        raise HTTPException(400, "at most 3 notes per chat")
    for i in ids:
        _load_note(i)
    with _db() as con:
        con.execute("UPDATE chats SET notes = ? WHERE id = ?", (json.dumps(ids), chat_id))
    _drop_agents(chat_id)
    return get_chat(chat_id)


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str):
    if chat_id in busy:
        raise HTTPException(409, "chat is generating")
    agents.pop(chat_id, None)
    with _db() as con:
        con.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return {"ok": True}


class Ask(BaseModel):
    content: str


@app.post("/api/chats/{chat_id}/stop")
def stop(chat_id: str):
    if chat_id in agents:
        agents[chat_id].cancel.set()
    return {"ok": True}


@app.post("/api/chats/{chat_id}/messages")
async def send(chat_id: str, body: Ask):
    if not body.content.strip():
        raise HTTPException(400, "empty message")
    if chat_id in busy:
        raise HTTPException(409, "chat is already generating")
    agent = _agent(chat_id)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(kind, data):
        loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

    def work():
        try:
            agent.ask(body.content.strip())
        except Exception as e:
            emit("error", f"{type(e).__name__}: {e}")
        finally:
            _save_chat(chat_id, agent.messages)
            busy.discard(chat_id)
            emit("done", None)

    def on_close():
        if chat_id in busy:   # client went away mid-answer
            agent.cancel.set()

    agent.on_event = emit
    busy.add(chat_id)
    threading.Thread(target=work, daemon=True).start()
    return _sse(queue, on_close)


BIND = {"host": "127.0.0.1", "port": 8120}   # where the server listens (shown on the API settings card)


def run(host="127.0.0.1", port=8120):
    import uvicorn
    BIND.update(host=host, port=port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


# external API + key management (see api_v1.py)
from .api_v1 import keys_admin, v1  # noqa: E402  (after app/helpers exist)

app.include_router(v1)
app.include_router(keys_admin)
