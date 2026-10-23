"""Adds files to the library: extract text -> chunk -> full-text index -> embed. Re-adding an unchanged file is a no-op."""
import hashlib
import json
import queue
import threading
import time
from pathlib import Path

import numpy as np

from . import config
from .extract import chunk, extract, supported
from .library import connect
from .llm import embed

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode"}


def sha256(path: Path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _embed_with_retry(texts):
    for attempt in range(6):   # Ollama occasionally fails transiently (e.g. port exhaustion on Windows)
        try:
            return np.asarray(embed(texts), dtype=np.float32)
        except Exception as e:
            if attempt == 5:
                raise
            print(f"\n  embed failed ({e}); retrying in {2 ** attempt}s")
            time.sleep(2 ** attempt)


def _embed_text(title, heading, text):
    head = title + (f" > {heading}" if heading and heading != title else "")
    return (config.EMBED_DOC_PREFIX + head + "\n" + text)[: config.EMBED_MAX_CHARS]


def ingest_file(path, collection=None, force=False, con=None, log=print):
    """Index one file. Returns (doc_id, outcome) with outcome 'added', 'updated', 'unchanged' or 'error'."""
    path = Path(path).resolve()
    own = con is None
    con = con or connect()
    try:
        st = path.stat()
        if st.st_size > config.MAX_FILE_MB * 2**20:
            raise ValueError(f"file is larger than {config.MAX_FILE_MB} MB (MSFRAG_MAX_FILE_MB)")
        digest = sha256(path)
        row = con.execute("SELECT id, sha256, status, collection FROM documents WHERE path = ?", (str(path),)).fetchone()
        if row and row["sha256"] == digest and row["status"] == "ready" and not force:
            if collection and collection != row["collection"]:
                con.execute("UPDATE documents SET collection = ? WHERE id = ?", (collection, row["id"]))
                con.commit()
            return row["id"], "unchanged"
        now = time.time()
        if row:
            doc_id = row["id"]
            con.execute("UPDATE documents SET status = 'indexing', error = NULL, size = ?, sha256 = ?, mtime = ?, "
                        "collection = coalesce(?, collection) WHERE id = ?",
                        (st.st_size, digest, st.st_mtime, collection, doc_id))
        else:
            doc_id = con.execute(
                "INSERT INTO documents (path, name, title, kind, collection, size, sha256, mtime, added, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'indexing')",
                (str(path), path.name, path.stem, path.suffix.lower().lstrip("."), collection,
                 st.st_size, digest, st.st_mtime, now)).lastrowid
        con.commit()
    except Exception as e:
        if own:
            con.close()
        log(f"  [red]error[/] {path.name}: {e}")
        return None, "error"

    try:
        _index(con, doc_id, path, log)
        return doc_id, "updated" if row else "added"
    except Exception as e:
        con.execute("UPDATE documents SET status = 'error', error = ? WHERE id = ?", (f"{type(e).__name__}: {e}", doc_id))
        con.commit()
        log(f"  [red]error[/] {path.name}: {type(e).__name__}: {e}")
        return doc_id, "error"
    finally:
        if own:
            con.close()


def _index(con, doc_id, path, log):
    t0 = time.time()
    ex = extract(path)
    title = (ex.title or "").strip() or path.stem
    pieces = chunk(ex.pages, config.CHUNK_CHARS, config.CHUNK_OVERLAP)
    pages = ex.meta.get("page_count") or ex.meta.get("slide_count") or (max((p for p, _ in ex.pages), default=0) or 1)

    old = [r[0] for r in con.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))]
    if old:
        con.execute(f"DELETE FROM vectors WHERE chunk_id IN ({','.join('?' * len(old))})", old)
        con.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    ids = []
    for seq, (page, heading, text) in enumerate(pieces):
        ids.append(con.execute("INSERT INTO chunks (doc_id, seq, page, heading, text) VALUES (?, ?, ?, ?, ?)",
                               (doc_id, seq, page, heading, text)).lastrowid)
    con.execute("UPDATE documents SET title = ?, kind = ?, pages = ?, chars = ?, chunks = ?, warning = ?, meta = ?, "
                "summary = NULL WHERE id = ?",
                (title, ex.kind, pages, ex.chars, len(pieces), ex.warning, json.dumps(ex.meta, default=str), doc_id))
    con.commit()   # keyword search works from here on, embeddings follow

    con.execute("INSERT OR IGNORE INTO meta VALUES ('embed_model', ?)", (config.EMBED_MODEL,))
    B = config.EMBED_BATCH
    for s in range(0, len(pieces), B):
        texts = [_embed_text(title, h, t) for _, h, t in pieces[s:s + B]]
        vecs = _embed_with_retry(texts)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
        con.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                        [(i, v.astype(np.float16).tobytes()) for i, v in zip(ids[s:s + B], vecs)])
        con.commit()
    con.execute("UPDATE documents SET status = 'ready' WHERE id = ?", (doc_id,))
    con.commit()
    note = f" [yellow]({ex.warning})[/]" if ex.warning else ""
    log(f"  [green]ok[/] D{doc_id} {path.name}: {pages} page(s), {len(pieces)} chunks, "
        f"{ex.chars:,} chars in {time.time() - t0:.1f}s{note}")


def walk(paths, recursive=True):
    """Supported files under the given files/folders."""
    for p in map(Path, paths):
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(it):
                if f.is_file() and supported(f) and not (set(f.relative_to(p).parts[:-1]) & SKIP_DIRS) \
                        and not f.name.startswith("~$"):
                    yield f
        elif p.is_file():
            yield p
        else:
            raise FileNotFoundError(p)


def remove(doc_id, con=None):
    own = con is None
    con = con or connect()
    try:
        row = con.execute("SELECT path FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return False
        ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))]
        if ids:
            con.execute(f"DELETE FROM vectors WHERE chunk_id IN ({','.join('?' * len(ids))})", ids)
        con.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        con.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        con.commit()
        p = Path(row["path"])
        if config.LIBRARY_DIR.resolve() in p.parents and p.exists():   # our copy of an uploaded file
            p.unlink()
        return True
    finally:
        if own:
            con.close()


def stored_upload_path(name):
    """Where an uploaded file is kept; never overwrites a different file of the same name."""
    config.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._- ()" else "_" for c in Path(name).name).strip(" .") or "upload"
    target = config.LIBRARY_DIR / safe
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        n += 1
        target = config.LIBRARY_DIR / f"{stem} ({n}){suffix}"
    return target


# ---- background worker (web app) -----------------------------------------------------------
class Worker:
    """One thread that indexes uploads and writes summaries, so the GPU is not shared by several jobs."""

    def __init__(self, on_indexed=None):
        self.jobs = queue.Queue()
        self.on_indexed = on_indexed or (lambda: None)
        self.current = None
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, path, collection=None, force=False):
        with connect() as con:   # show it in the library right away
            p = Path(path).resolve()
            if not con.execute("SELECT 1 FROM documents WHERE path = ?", (str(p),)).fetchone():
                st = p.stat()
                con.execute("INSERT INTO documents (path, name, title, kind, collection, size, mtime, added, status) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
                            (str(p), p.name, p.stem, p.suffix.lower().lstrip("."), collection, st.st_size,
                             st.st_mtime, time.time()))
            else:
                con.execute("UPDATE documents SET status = 'queued' WHERE path = ? AND status != 'ready'", (str(p),))
        self.jobs.put(("ingest", str(p), collection, force))

    def summarize(self, doc_id):
        self.jobs.put(("summarize", doc_id))

    def pending(self):
        return self.jobs.qsize() + (1 if self.current else 0)

    def _run(self):
        from .analyze import summarize_document
        log = lambda msg: None   # noqa: E731  (the UI shows status from the database)
        while True:
            job = self.jobs.get()
            self.current = job
            try:
                if job[0] == "ingest":
                    _, path, collection, force = job
                    doc_id, outcome = ingest_file(path, collection, force, log=log)
                    if outcome in ("added", "updated"):
                        self.on_indexed()
                        if config.AUTO_SUMMARY:
                            self.jobs.put(("summarize", doc_id))
                elif job[0] == "summarize":
                    summarize_document(job[1])
            except Exception as e:
                print(f"background job {job[0]} failed: {type(e).__name__}: {e}")
            finally:
                self.current = None

