"""The document library: library.db holds documents, their chunks, an FTS5 index and the embeddings.

Documents are referred to as D<id> (e.g. D12) everywhere: tool results, citations and the UI.
"""
import json
import re
import sqlite3
from dataclasses import dataclass

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE,            -- absolute path of the indexed file
    name TEXT,                   -- file name
    title TEXT,                  -- from document metadata / first heading, else the file name
    kind TEXT,                   -- pdf, docx, markdown, ...
    collection TEXT,             -- user-chosen grouping, e.g. 'contracts'
    size INTEGER, sha256 TEXT, mtime REAL, added REAL,
    pages INTEGER, chars INTEGER, chunks INTEGER DEFAULT 0,
    status TEXT,                 -- queued, indexing, ready, error
    error TEXT, warning TEXT,
    summary TEXT,                -- LLM-written, optional
    meta TEXT                    -- JSON: author, dates, ...
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER, seq INTEGER, page INTEGER, heading TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading, content='chunks', content_rowid='id', tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading) VALUES ('delete', old.id, old.text, old.heading);
END;
CREATE TABLE IF NOT EXISTS vectors (chunk_id INTEGER PRIMARY KEY, emb BLOB);
"""

HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)
DOC_REF_RE = re.compile(r"^\s*D?(\d+)\s*$", re.I)
WORD_RE = re.compile(r"[\w][\w.\-]*", re.UNICODE)
STOP = set("a an and are as at be by for from has have how i in is it of on or that the this to was were what "
           "when where which who why with does do did about into can could should would will".split())


def connect(readonly=False):
    if readonly:
        con = sqlite3.connect(f"file:{config.LIBRARY_DB}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        con = sqlite3.connect(config.LIBRARY_DB, timeout=30, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")   # readers (web app) are not blocked while indexing
        con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


@dataclass
class Filters:
    documents: str | None = None    # D-ids or file/title fragments, comma = any of
    collection: str | None = None
    kind: str | None = None

    @classmethod
    def from_args(cls, a: dict):
        def s(k):
            v = a.get(k)
            if isinstance(v, list):
                v = ",".join(str(x) for x in v)
            return str(v).strip() if v not in (None, "") else None
        return cls(s("document") or s("documents"), s("collection"), s("kind"))

    def __bool__(self):
        return bool(self.documents or self.collection or self.kind)


class Library:
    def __init__(self):
        connect().close()   # create the schema on first run
        self.conn = connect()

    # ---- filters -------------------------------------------------------------------------
    def doc_ids(self, f: Filters):
        """Document ids matching the filters, or None when there are no filters."""
        if not f:
            return None
        where, args = ["status = 'ready'"], []
        if f.documents:
            ors = []
            for part in (p.strip() for p in f.documents.split(",") if p.strip()):
                m = DOC_REF_RE.match(part)
                if m:
                    ors.append("id = ?"); args.append(int(m.group(1)))
                else:
                    ors.append("(name LIKE ? OR title LIKE ?)"); args += [f"%{part}%"] * 2
            where.append("(" + " OR ".join(ors) + ")")
        if f.collection:
            where.append("collection = ? COLLATE NOCASE"); args.append(f.collection)
        if f.kind:
            where.append("kind = ? COLLATE NOCASE"); args.append(f.kind.lower().lstrip("."))
        return [r[0] for r in self.conn.execute(f"SELECT id FROM documents WHERE {' AND '.join(where)}", args)]

    def chunk_ids(self, doc_ids):
        if doc_ids is None:
            return None
        if not doc_ids:
            return set()
        q = f"SELECT id FROM chunks WHERE doc_id IN ({','.join('?' * len(doc_ids))})"
        return {r[0] for r in self.conn.execute(q, doc_ids)}

    # ---- search --------------------------------------------------------------------------
    @staticmethod
    def fts_query(text):
        words = [w for w in WORD_RE.findall(text.lower()) if w not in STOP and len(w) > 1]
        return " OR ".join('"' + w.replace('"', "") + '"' for w in dict.fromkeys(words))

    def fts_search(self, query, doc_ids, k):
        q = self.fts_query(query)
        if not q:
            return []
        sql = ("SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
               "JOIN documents d ON d.id = c.doc_id WHERE chunks_fts MATCH ? AND d.status = 'ready'")
        args = [q]
        if doc_ids is not None:
            if not doc_ids:
                return []
            sql += f" AND c.doc_id IN ({','.join('?' * len(doc_ids))})"
            args += doc_ids
        sql += " ORDER BY bm25(chunks_fts, 1.0, 2.0) LIMIT ?"
        try:
            return [r[0] for r in self.conn.execute(sql, args + [k])]
        except sqlite3.OperationalError:
            return []

    def passages(self, chunk_ids, max_chars=900):
        if not chunk_ids:
            return []
        rows = {r["id"]: r for r in self.conn.execute(
            f"SELECT c.id, c.doc_id, c.seq, c.page, c.heading, c.text, d.title, d.kind, d.pages "
            f"FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({','.join('?' * len(chunk_ids))})",
            list(chunk_ids))}
        out = []
        for cid in chunk_ids:
            r = rows.get(cid)
            if r is None:
                continue
            text = r["text"] if len(r["text"]) <= max_chars else r["text"][:max_chars] + "…"
            p = {"ref": f"D{r['doc_id']}", "doc_id": r["doc_id"], "title": r["title"]}
            if (r["pages"] or 0) > 1:   # no page key at all for page-less formats, so the model doesn't cite "p.0"
                p["page"] = r["page"]
            p.update(chunk=r["seq"], section=r["heading"], text=text)
            out.append(p)
        return out

    # ---- documents ----------------------------------------------------------------------
    def _doc_row(self, r, summary_chars=300):
        d = {"ref": f"D{r['id']}", "id": r["id"], "title": r["title"], "file": r["name"], "kind": r["kind"],
             "collection": r["collection"], "pages": r["pages"], "chunks": r["chunks"], "chars": r["chars"],
             "added": r["added"], "status": r["status"]}
        if r["summary"]:
            s = r["summary"]
            d["summary"] = s if summary_chars is None or len(s) <= summary_chars else s[:summary_chars] + "…"
        if r["warning"]:
            d["warning"] = r["warning"]
        if r["error"]:
            d["error"] = r["error"]
        return d

    def list_documents(self, query=None, f: Filters | None = None, limit=50, include_all=False):
        ids = self.doc_ids(f) if f else None
        where, args = [], []
        if not include_all:
            where.append("status = 'ready'")
        if ids is not None:
            if not ids:
                return []
            where.append(f"id IN ({','.join('?' * len(ids))})"); args += ids
        if query:
            where.append("(title LIKE ? OR name LIKE ? OR summary LIKE ?)"); args += [f"%{query}%"] * 3
        sql = "SELECT * FROM documents" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY added DESC LIMIT ?"
        return [self._doc_row(r) for r in self.conn.execute(sql, args + [limit])]

    def get_document(self, ref):
        m = DOC_REF_RE.match(str(ref))
        if not m:
            ids = self.doc_ids(Filters(documents=str(ref)))
            if not ids:
                return None
            doc_id = ids[0]
        else:
            doc_id = int(m.group(1))
        r = self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if r is None:
            return None
        d = self._doc_row(r, summary_chars=None)
        d.update(path=r["path"], size=r["size"], meta=json.loads(r["meta"] or "{}"))
        outline, seen = [], set()
        for c in self.conn.execute("SELECT seq, page, heading, text FROM chunks WHERE doc_id = ? ORDER BY seq", (doc_id,)):
            heads = [c["heading"]] if c["heading"] else []
            heads += [m.group(2).strip() for m in HEADING_LINE_RE.finditer(c["text"])]
            for h in heads:
                if (h, c["page"]) not in seen:   # chunk overlap repeats headings
                    seen.add((h, c["page"]))
                    outline.append({"section": h[:200], "page": c["page"], "chunk": c["seq"]})
        d["outline"] = outline[:80]
        if len(outline) > 80:
            d["outline_truncated"] = len(outline)
        return d

    def read(self, doc_id, start=0, count=6, page=None, max_chars=12000):
        """Consecutive chunks of one document (the overlap between chunks is not removed)."""
        total = self.conn.execute("SELECT count(*) FROM chunks WHERE doc_id = ?", (doc_id,)).fetchone()[0]
        if page is not None:
            r = self.conn.execute("SELECT min(seq) FROM chunks WHERE doc_id = ? AND page >= ?",
                                  (doc_id, page)).fetchone()
            start = r[0] if r and r[0] is not None else total
        rows = self.conn.execute("SELECT seq, page, heading, text FROM chunks WHERE doc_id = ? AND seq >= ? "
                                 "ORDER BY seq LIMIT ?", (doc_id, start, max(1, min(count, 20)))).fetchall()
        out, used = [], 0
        for r in rows:
            if used and used + len(r["text"]) > max_chars:
                break
            out.append({"chunk": r["seq"], "page": r["page"], "section": r["heading"], "text": r["text"]})
            used += len(r["text"])
        nxt = out[-1]["chunk"] + 1 if out else None
        return {"total_chunks": total, "chunks": out,
                "next_chunk": nxt if nxt is not None and nxt < total else None}

    def full_text(self, doc_id):
        return [(r["page"], r["heading"], r["text"]) for r in self.conn.execute(
            "SELECT page, heading, text FROM chunks WHERE doc_id = ? ORDER BY seq", (doc_id,))]

    def collections(self):
        return [{"collection": r[0], "documents": r[1]} for r in self.conn.execute(
            "SELECT coalesce(collection, ''), count(*) FROM documents WHERE status = 'ready' "
            "GROUP BY 1 ORDER BY 2 DESC")]

    def stats(self):
        r = self.conn.execute("SELECT count(*), coalesce(sum(chunks), 0), coalesce(sum(chars), 0), "
                              "sum(status = 'ready'), sum(status IN ('queued', 'indexing')), sum(status = 'error') "
                              "FROM documents").fetchone()
        kinds = dict(self.conn.execute("SELECT kind, count(*) FROM documents WHERE status = 'ready' GROUP BY kind"))
        return {"documents": r[0], "ready": r[3] or 0, "pending": r[4] or 0, "errors": r[5] or 0,
                "chunks": r[1], "chars": r[2], "kinds": kinds,
                "vectors": self.conn.execute("SELECT count(*) FROM vectors").fetchone()[0]}
