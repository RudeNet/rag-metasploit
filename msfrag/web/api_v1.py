"""External API (/api/v1) for scripts and other programs. Every call needs an API key.

Auth:  Authorization: Bearer msf_...   (or header  X-API-Key: msf_...)
Keys are stored as SHA-256 hashes in chats.db; the full key is shown only once, when created.
"""
import asyncio
import hashlib
import json
import secrets
import threading
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__, config
from ..library import Filters

KEY_PREFIX = "msf_"

v1 = APIRouter(prefix="/api/v1", tags=["API v1"])
keys_admin = APIRouter(prefix="/api/keys", tags=["API keys (UI)"])


def _srv():
    from . import server   # imported lazily: server imports this module
    return server


def _keys_db():
    con = _srv()._db()
    con.execute("CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, name TEXT, prefix TEXT, "
                "hash TEXT UNIQUE, created REAL, last_used REAL, uses INTEGER DEFAULT 0)")
    return con


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ---- auth --------------------------------------------------------------------------------
def require_key(authorization: str | None = Header(None), x_api_key: str | None = Header(None)):
    if not config.API_ENABLED:
        raise HTTPException(403, "API access is disabled - enable it in Metasploit API Explorer Settings -> API")
    key = x_api_key or (authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ")
                        else None)
    if not key:
        raise HTTPException(401, "missing API key: send 'Authorization: Bearer msf_...' or 'X-API-Key: msf_...'",
                            headers={"WWW-Authenticate": "Bearer"})
    with _keys_db() as con:
        row = con.execute("SELECT id FROM api_keys WHERE hash = ?", (_hash(key),)).fetchone()
        if row is None:
            raise HTTPException(401, "invalid or revoked API key", headers={"WWW-Authenticate": "Bearer"})
        con.execute("UPDATE api_keys SET last_used = ?, uses = uses + 1 WHERE id = ?", (time.time(), row["id"]))
    return row["id"]


# ---- key management (used by the Settings page, same-origin UI) ---------------------------
class NewKey(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


@keys_admin.get("")
def list_keys():
    with _keys_db() as con:
        rows = con.execute("SELECT id, name, prefix, created, last_used, uses FROM api_keys "
                           "ORDER BY created DESC").fetchall()
    bind = _srv().BIND
    return {"enabled": config.API_ENABLED, "keys": [dict(r) for r in rows], "bind_host": bind["host"],
            "port": bind["port"], "local_only": bind["host"] in ("127.0.0.1", "localhost", "::1")}


@keys_admin.post("")
def create_key(body: NewKey):
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    row = {"id": uuid.uuid4().hex[:12], "name": body.name.strip(), "prefix": key[:10],
           "hash": _hash(key), "created": time.time()}
    with _keys_db() as con:
        con.execute("INSERT INTO api_keys (id, name, prefix, hash, created) "
                    "VALUES (:id, :name, :prefix, :hash, :created)", row)
    return {"id": row["id"], "name": row["name"], "key": key}   # the only time the full key is returned


@keys_admin.delete("/{key_id}")
def revoke_key(key_id: str):
    with _keys_db() as con:
        if con.execute("DELETE FROM api_keys WHERE id = ?", (key_id,)).rowcount == 0:
            raise HTTPException(404, "key not found")
    return {"ok": True}


# ---- v1 endpoints -------------------------------------------------------------------------
@v1.get("/health", summary="Service status")
def health(_=Depends(require_key)):
    srv = _srv()
    with srv.tool_lock:
        st = srv.retriever.db.stats()
    return {"status": "ok", "version": __version__, "chat_model": config.CHAT_MODEL,
            "documents": st["ready"], "chunks": st["chunks"], "vectors": len(srv.retriever.vectors),
            "jobs": srv.worker.pending()}


FILTER_DOC = ("Filters: document (refs like D3 or file/title words, comma = any of), collection, "
              "kind (pdf, docx, pptx, xlsx, markdown, text, html, csv, email, ...)")


@v1.get("/search", summary="Search passages (hybrid keyword + semantic, no LLM)", description=FILTER_DOC)
def search(q: str = Query("", description="Query text; empty = the beginning of each matching document"),
           limit: int = Query(10, ge=1, le=100), mode: str = Query("hybrid", pattern="^(hybrid|keyword|semantic)$"),
           document: str | None = None, collection: str | None = None, kind: str | None = None,
           _=Depends(require_key)):
    srv = _srv()
    with srv.tool_lock:
        hits = srv.retriever.search(q, Filters(document, collection, kind), limit, mode)
    return {"count": len(hits), "results": hits}


@v1.get("/documents", summary="List documents", description=FILTER_DOC)
def documents(q: str | None = Query(None, description="Words in title, file name or summary"),
              collection: str | None = None, kind: str | None = None, limit: int = Query(100, ge=1, le=2000),
              _=Depends(require_key)):
    srv = _srv()
    with srv.tool_lock:
        docs = srv.retriever.db.list_documents(q, Filters(collection=collection, kind=kind), limit, include_all=True)
    return {"count": len(docs), "documents": docs}


@v1.get("/documents/{ref}", summary="One document: metadata, summary, outline")
def document(ref: str, _=Depends(require_key)):
    return _srv()._doc(ref)


@v1.get("/documents/{ref}/text", summary="Read a document's text in order (paged by chunk)")
def document_text(ref: str, chunk: int = Query(0, ge=0), count: int = Query(20, ge=1, le=20),
                  page: int | None = None, _=Depends(require_key)):
    return _srv().read_document(ref, page, chunk, count)


@v1.post("/documents", summary="Upload a document (raw body) - indexed in the background")
async def upload(request: Request, name: str = Query(..., description="File name incl. extension, e.g. report.pdf"),
                 collection: str | None = None, _=Depends(require_key)):
    return await _srv().upload_document(request, name, collection)


@v1.delete("/documents/{ref}", summary="Remove a document from the library")
def delete(ref: str, _=Depends(require_key)):
    return _srv().delete_document(ref)


class AskBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=20000)
    chat_id: str | None = Field(None, description="Continue an existing conversation (from a previous response)")
    save: bool = Field(False, description="Store the conversation as a chat (visible in the UI) and return its "
                                          "chat_id, so follow-up questions can continue it")
    stream: bool = Field(False, description="Return server-sent events (token, tool_call, tool_result, ...) "
                                            "instead of one JSON response")


@v1.post("/ask", summary="Ask the agent (tool-calling LLM over the document library)")
async def ask(body: AskBody, _=Depends(require_key)):
    srv = _srv()
    chat_id = body.chat_id
    if chat_id:
        srv._load_chat(chat_id)   # 404 if unknown
    elif body.save:
        chat_id = srv.create_chat()["id"]
    if chat_id and chat_id in srv.busy:
        raise HTTPException(409, "this chat is already answering a question")

    agent = srv._agent(chat_id) if chat_id else srv.new_agent()   # one-off question: nothing stored

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    tools = []

    def emit(kind, data):
        if kind == "tool_call":
            tools.append({"name": data["name"], "arguments": data.get("arguments") or {}})
        loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

    result = {}

    def work():
        try:
            result["answer"] = agent.ask(body.question.strip())
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            emit("error", result["error"])
        finally:
            if chat_id:
                srv._save_chat(chat_id, agent.messages)
                srv.busy.discard(chat_id)
            emit("done", {"chat_id": chat_id})

    agent.on_event = emit
    if chat_id:
        srv.busy.add(chat_id)
    threading.Thread(target=work, daemon=True).start()

    if body.stream:
        async def events():
            try:
                while True:
                    kind, data = await queue.get()
                    yield f"data: {json.dumps({'type': kind, 'data': data}, default=str)}\n\n"
                    if kind == "done":
                        break
            finally:
                if chat_id is None or chat_id in srv.busy:
                    agent.cancel.set()   # client disconnected mid-answer
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    while True:   # non-streaming: wait for completion
        kind, _data = await queue.get()
        if kind == "done":
            break
    if "error" in result:
        raise HTTPException(502, result["error"])
    stats_ = next((m.get("stats") for m in reversed(agent.messages) if m.get("stats")), None)
    return {"answer": result.get("answer", ""), "chat_id": chat_id, "model": config.CHAT_MODEL,
            "tool_calls": tools, "stats": stats_, "context": agent.context_info}
