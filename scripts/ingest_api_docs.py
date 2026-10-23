#!/usr/bin/env python3
"""Ingest the scraped Metasploit API reference into the msfrag library.

Each Markdown page becomes one document; its `collection` is set to the top-level namespace
(the first folder under the corpus, e.g. "Msf", "Rex", "Msf::Exploit"), so questions can be
restricted to a namespace. Unchanged files are skipped (SHA-256), so re-running is cheap.

    python scripts/ingest_api_docs.py [CORPUS_DIR] [--no-summary] [--limit N]

Needs Ollama running with the embedding model pulled (see `python -m msfrag doctor`).
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from msfrag import config, settings          # noqa: E402
from msfrag.analyze import summarize_document  # noqa: E402
from msfrag.ingest import ingest_file          # noqa: E402
from msfrag.library import connect             # noqa: E402


def namespace_of(path: Path, corpus: Path) -> str:
    """Top-level namespace from the file's location, as a Ruby-style name (Msf, Rex, Msf::Exploit...)."""
    rel = path.relative_to(corpus)
    parts = rel.parts[:-1]                      # drop the file name
    if not parts:
        return "(top level)"
    # one level of nesting is enough to group usefully without hundreds of tiny collections
    return parts[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", nargs="?", default=str(ROOT / "metasploit-api-docs"))
    ap.add_argument("--no-summary", action="store_true", help="skip LLM summaries (much faster)")
    ap.add_argument("--limit", type=int, default=0, help="ingest at most N files (for a quick test)")
    ap.add_argument("--force", action="store_true", help="re-index even if unchanged")
    a = ap.parse_args(argv)
    settings.load()

    corpus = Path(a.corpus).resolve()
    files = sorted(p for p in corpus.rglob("*.md") if not p.name.startswith("_"))
    if a.limit:
        files = files[: a.limit]
    if not files:
        print(f"no .md files under {corpus}"); return 1
    print(f"Ingesting {len(files)} pages from {corpus} with {config.EMBED_MODEL} "
          f"(summaries: {'off' if a.no_summary else 'on'})")

    t0 = time.time()
    counts, new = {}, []
    con = connect()
    try:
        quiet = lambda *args, **kw: None   # noqa: E731  (we print our own progress)
        for i, f in enumerate(files, 1):
            doc_id, outcome = ingest_file(f, collection=namespace_of(f, corpus), force=a.force, con=con, log=quiet)
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome in ("added", "updated"):
                new.append(doc_id)
            if i % 50 == 0 or i == len(files):
                rate = i / (time.time() - t0)
                print(f"  {i}/{len(files)}  ({', '.join(f'{n} {k}' for k, n in counts.items())})  {rate:.1f}/s")
    finally:
        con.close()

    if new and not a.no_summary:
        print(f"Writing summaries for {len(new)} pages with {config.CHAT_MODEL} (skip with --no-summary)…")
        for j, doc_id in enumerate(new, 1):
            try:
                summarize_document(doc_id)
            except Exception as e:
                print(f"  summary failed for D{doc_id}: {e}")
            if j % 50 == 0 or j == len(new):
                print(f"  summarized {j}/{len(new)}")
    print(f"done in {time.time() - t0:.0f}s: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
