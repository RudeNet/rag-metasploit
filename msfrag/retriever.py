"""Hybrid retrieval: FTS5/BM25 keyword search + embedding search, merged with reciprocal rank fusion."""
import threading

from . import config
from .index import VectorStore
from .library import Filters, Library
from .llm import embed_query


class Retriever:
    def __init__(self):
        self.db = Library()
        self.vectors = VectorStore()
        self._lock = threading.Lock()

    def reload_vectors(self):
        vs = VectorStore()
        with self._lock:
            self.vectors = vs
        return len(vs)

    def search(self, query, filters: Filters, limit=10, mode="hybrid"):
        query = (query or "").strip()
        doc_ids = self.db.doc_ids(filters)
        if not query:
            # no query: the beginning of each matching document
            ids = doc_ids if doc_ids is not None else [d["id"] for d in self.db.list_documents(limit=limit)]
            first = [self.db.conn.execute("SELECT id FROM chunks WHERE doc_id = ? ORDER BY seq LIMIT 1", (i,)).fetchone()
                     for i in ids[:limit]]
            return self.db.passages([r[0] for r in first if r])

        ranked = []
        if mode in ("hybrid", "keyword"):
            ranked.append(self.db.fts_search(query, doc_ids, config.CANDIDATES))
        vectors = self.vectors
        if mode in ("hybrid", "semantic") and len(vectors):
            ranked.append(vectors.search(embed_query(query), config.CANDIDATES, self.db.chunk_ids(doc_ids)))

        scores = {}
        for lst in ranked:
            for rank, cid in enumerate(lst):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + rank)
        fused = sorted(scores, key=scores.get, reverse=True)
        return self.db.passages(fused[:limit])
