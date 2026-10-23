"""In-memory vector search over the chunk embeddings stored in library.db."""
import sqlite3

import numpy as np

from . import config


def check_model(con):
    """The stored embeddings must come from the configured embedding model."""
    row = con.execute("SELECT v FROM meta WHERE k = 'embed_model'").fetchone()
    if row and row[0] != config.EMBED_MODEL:
        raise SystemExit(f"Library was embedded with {row[0]!r} but MSFRAG_EMBED_MODEL={config.EMBED_MODEL!r}; "
                         f"run `python -m msfrag reindex` or switch models back.")


class VectorStore:
    """All embeddings held in RAM as float16; cosine similarity via chunked matmul."""

    def __init__(self, db=None):
        self.ids = np.zeros(0, dtype=np.int64)
        self.matrix = None
        db = db or config.LIBRARY_DB
        if not db.exists():
            return
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            check_model(con)
            rows = con.execute("SELECT chunk_id, emb FROM vectors ORDER BY chunk_id").fetchall()
        except sqlite3.OperationalError:   # schema not created yet
            rows = []
        finally:
            con.close()
        if not rows:
            return
        self.ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        self.matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1)

    def __len__(self):
        return len(self.ids)

    def search(self, qvec, k, candidates=None):
        if self.matrix is None:
            return []
        q = np.asarray(qvec, dtype=np.float32)
        q /= np.linalg.norm(q) + 1e-12
        if candidates is not None:
            cand = np.asarray(sorted(candidates), dtype=np.int64)
            if len(cand) == 0:
                return []
            pos = np.minimum(np.searchsorted(self.ids, cand), len(self.ids) - 1)
            pos = pos[self.ids[pos] == cand]   # drop candidates that have no embedding yet
            if len(pos) == 0:
                return []
            scores = self.matrix[pos].astype(np.float32) @ q
            top = np.argsort(-scores)[:k]
            return [int(self.ids[pos[i]]) for i in top]
        best_s, best_i = [], []
        step = 50_000
        for s in range(0, len(self.ids), step):
            sc = self.matrix[s:s + step].astype(np.float32) @ q
            kk = min(k, len(sc))
            part = np.argpartition(-sc, kk - 1)[:kk]
            best_s.append(sc[part]); best_i.append(part + s)
        sc, ix = np.concatenate(best_s), np.concatenate(best_i)
        order = np.argsort(-sc)[:k]
        return [int(self.ids[ix[i]]) for i in order]
