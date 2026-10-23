"""CLI: python -m msfrag {doctor,add,list,remove,sync,summarize,search,ask,chat,web,reindex}"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__, config

console = Console()


def cmd_doctor(_):
    from .library import Library
    from .llm import client
    ok = True
    console.print(f"[bold]Metasploit API Explorer {__version__}[/]")
    st = Library().stats()
    console.print(f"library:   {config.LIBRARY_DB} [green]ok[/] - {st['ready']} documents ready, "
                  f"{st['chunks']:,} chunks, {st['vectors']:,} embedded"
                  + (f", [yellow]{st['pending']} pending[/]" if st["pending"] else "")
                  + (f", [red]{st['errors']} with errors[/]" if st["errors"] else ""))
    if shutil.which("nvidia-smi"):
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,driver_version",
                              "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
        console.print(f"GPU:       [green]{gpu}")
    else:
        console.print("GPU:       [yellow]nvidia-smi not found - Ollama may fall back to CPU")
    try:
        models = {m.model for m in client().list().models}
        console.print(f"Ollama:    [green]{config.OLLAMA_HOST} reachable[/] ({len(models)} models)")
    except Exception as e:
        console.print(f"Ollama:    [red]not reachable at {config.OLLAMA_HOST}: {e}")
        return 1
    for m in (config.CHAT_MODEL, config.EMBED_MODEL):
        have = m in models or f"{m}:latest" in models
        console.print(f"  {m:<28} {'[green]pulled' if have else f'[red]missing -> ollama pull {m}'}")
        ok &= have
    for p in client().ps().models:
        vram, total = p.size_vram or 0, p.size or 1
        console.print(f"  loaded {p.model:<21} {vram / total:6.0%} in VRAM "
                      f"({vram / 2**30:.1f} / {total / 2**30:.1f} GiB)")
    return 0 if ok else 1


def cmd_add(a):
    from .analyze import summarize_document
    from .ingest import ingest_file, walk
    try:
        files = list(walk(a.paths, recursive=not a.no_recursive))
    except FileNotFoundError as e:
        console.print(f"[red]not found:[/] {e}"); return 1
    if not files:
        console.print("[yellow]no supported files found[/]"); return 1
    console.print(f"Indexing {len(files)} file(s) with {config.EMBED_MODEL}"
                  + (f" into collection [bold]{a.collection}[/]" if a.collection else ""))
    counts, new = {}, []
    for f in files:
        doc_id, outcome = ingest_file(f, a.collection, a.force, log=console.print)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in ("added", "updated"):
            new.append(doc_id)
    console.print(", ".join(f"{n} {k}" for k, n in counts.items()))
    summarize = config.AUTO_SUMMARY if a.summarize is None else a.summarize
    if new and summarize:
        console.print(f"Writing summaries with {config.CHAT_MODEL} (skip with --no-summary) ...")
        for doc_id in new:
            try:
                summarize_document(doc_id)
                console.print(f"  [green]summary[/] D{doc_id}")
            except Exception as e:
                console.print(f"  [yellow]summary failed for D{doc_id}: {e}")
    return 1 if counts.get("error") else 0


def cmd_list(a):
    from .library import Filters, Library
    docs = Library().list_documents(a.query, Filters(collection=a.collection, kind=a.kind), a.limit, include_all=True)
    if a.json:
        print(json.dumps(docs, indent=2, default=str)); return
    t = Table()
    for col in ("Ref", "Title", "Type", "Pages", "Chunks", "Collection", "Status"):
        t.add_column(col, overflow="fold")
    for d in docs:
        status = d["status"] + (f" ({d['error']})" if d.get("error") else "") + (" ⚠" if d.get("warning") else "")
        t.add_row(d["ref"], d["title"], d["kind"] or "", str(d["pages"] or ""), str(d["chunks"] or ""),
                  d["collection"] or "", status)
    console.print(t)


def cmd_show(a):
    from .library import Library
    d = Library().get_document(a.document)
    if d is None:
        console.print(f"[red]{a.document} not found"); return 1
    if a.json:
        print(json.dumps(d, indent=2, default=str)); return
    console.print(f"[bold]{d['ref']} {d['title']}[/]  ({d['kind']}, {d['pages']} pages, {d['chunks']} chunks)")
    console.print(f"[dim]{d['path']}[/]")
    if d.get("summary"):
        console.print("\n" + d["summary"])
    if d["outline"]:
        console.print("\n[bold]Outline[/]")
        for o in d["outline"]:
            console.print(f"  p.{o['page']}  {o['section']}")


def cmd_remove(a):
    from .ingest import remove
    from .library import Library
    lib = Library()
    for ref in a.documents:
        d = lib.get_document(ref)
        if d is None:
            console.print(f"[red]{ref} not found"); continue
        remove(d["id"])
        console.print(f"removed {d['ref']} {d['title']}")


def cmd_sync(a):
    """Re-index changed files; report (or with --prune remove) documents whose file is gone."""
    from .ingest import ingest_file, remove
    from .library import connect
    with connect() as con:
        rows = con.execute("SELECT id, path, title FROM documents ORDER BY id").fetchall()
    counts = {}
    for r in rows:
        if not Path(r["path"]).exists():
            if a.prune:
                remove(r["id"]); counts["removed"] = counts.get("removed", 0) + 1
                console.print(f"  removed D{r['id']} {r['title']} (file gone)")
            else:
                counts["missing"] = counts.get("missing", 0) + 1
                console.print(f"  [yellow]missing[/] D{r['id']} {r['path']}")
            continue
        _, outcome = ingest_file(r["path"], log=console.print)
        counts[outcome] = counts.get(outcome, 0) + 1
    console.print(", ".join(f"{n} {k}" for k, n in counts.items()) or "library is empty")


def cmd_reindex(a):
    """Re-embed everything (after changing the embedding model or chunk size)."""
    from .ingest import ingest_file
    from .library import connect
    with connect() as con:
        con.execute("DELETE FROM meta WHERE k = 'embed_model'")
        rows = con.execute("SELECT path FROM documents ORDER BY id").fetchall()
    for r in rows:
        ingest_file(r["path"], force=True, log=console.print)


def cmd_summarize(a):
    from .analyze import summarize_document
    from .library import Library
    d = Library().get_document(a.document)
    if d is None:
        console.print(f"[red]{a.document} not found"); return 1
    with console.status(f"Summarizing {d['ref']} {d['title']} with {config.CHAT_MODEL} ..."):
        console.print(summarize_document(d["id"]))


def cmd_search(a):
    from .library import Filters
    from .retriever import Retriever
    hits = Retriever().search(a.query, Filters(a.document, a.collection, a.kind), a.limit, a.mode)
    if a.json:
        print(json.dumps(hits, indent=2)); return
    t = Table(show_lines=True)
    for col in ("Ref", "Page", "Title / section", "Passage"):
        t.add_column(col, overflow="fold")
    for h in hits:
        t.add_row(h["ref"], str(h.get("page") or ""), h["title"] + (f"\n[dim]{h['section']}[/]" if h["section"] else ""),
                  h["text"][:300])
    console.print(t)


def _event_printer(verbose):
    state = {"streaming": False}

    def on_event(kind, data):
        if kind == "tool_call":
            console.print(f"[cyan]> {data['name']}[/] {json.dumps(data.get('arguments') or {})}")
        elif kind == "tool_result" and verbose:
            console.print(f"[dim]{json.dumps(data['result'], default=str)[:1500]}[/]")
        elif kind == "progress":
            console.print(f"[dim]  {data}[/]")
        elif kind == "thinking" and verbose:
            console.print(data, end="", style="dim italic", markup=False, highlight=False)
        elif kind == "token":
            state["streaming"] = True
            console.print(data, end="", markup=False, highlight=False)
        elif kind == "turn_end" and state["streaming"]:
            console.print(); state["streaming"] = False
        elif kind == "warning":
            console.print(f"[yellow]{data}")

    return on_event


def _make_agent(verbose):
    from .agent import Agent
    return Agent(_event_printer(verbose))


def cmd_ask(a):
    _make_agent(a.verbose).ask(a.question)


def cmd_chat(a):
    agent = _make_agent(a.verbose)
    st = agent.db.stats()
    console.print(f"[bold]Document RAG agent[/] - model {config.CHAT_MODEL}, embeddings {config.EMBED_MODEL}, "
                  f"{st['ready']} documents, {len(agent.retriever.vectors):,} vectors loaded")
    console.print("[dim]/context shows memory, /reset clears history, /verbose toggles tool output, /exit quits[/]")
    while True:
        try:
            q = console.input("\n[bold green]you> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q in ("/exit", "/quit"):
            break
        if q == "/context":
            console.print(agent.context_info or "[dim]empty[/]")
            for ref, item in list(agent.memory.items.items())[-15:]:
                console.print(f"  [cyan]{ref}[/] {agent.memory.line(item)}", highlight=False)
            continue
        if q == "/reset":
            agent.reset(); console.print("[dim]history cleared[/]"); continue
        if q == "/verbose":
            a.verbose = not a.verbose
            agent.on_event = _event_printer(a.verbose)
            console.print(f"[dim]verbose {'on' if a.verbose else 'off'}[/]"); continue
        try:
            agent.ask(q)
        except KeyboardInterrupt:
            console.print("\n[yellow]interrupted[/]")


def cmd_web(a):
    from .web.server import run
    console.print(f"[bold]Metasploit API Explorer {__version__}[/] - built by rudenetworks · http://{a.host}:{a.port}")
    run(a.host, a.port)


def main(argv=None):
    from .extract import SUPPORTED
    p = argparse.ArgumentParser(prog="msfrag", description="Local RAG agent over the Metasploit Framework API reference (Ollama + CUDA)")
    p.add_argument("--version", action="version", version=f"Metasploit API Explorer {__version__} - built by rudenetworks (rudenetworks.com)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check library, Ollama, models and GPU").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("add", help="index files or folders", description="Supported: " + " ".join(SUPPORTED))
    s.add_argument("paths", nargs="+")
    s.add_argument("-c", "--collection", help="group the documents, e.g. 'contracts' (filterable)")
    s.add_argument("--force", action="store_true", help="re-index even if unchanged")
    s.add_argument("--no-recursive", action="store_true", help="do not descend into sub-folders")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--summary", dest="summarize", action="store_true", default=None, help="write LLM summaries")
    g.add_argument("--no-summary", dest="summarize", action="store_false", help="skip LLM summaries (faster)")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("list", help="list documents")
    s.add_argument("query", nargs="?")
    s.add_argument("-c", "--collection")
    s.add_argument("--kind")
    s.add_argument("-n", "--limit", type=int, default=200)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="details, summary and outline of one document")
    s.add_argument("document", help="ref like D3, or a title fragment")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("remove", help="remove documents from the library (files are not deleted)")
    s.add_argument("documents", nargs="+", help="refs like D3")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("sync", help="re-index changed files, report missing ones")
    s.add_argument("--prune", action="store_true", help="remove documents whose file no longer exists")
    s.set_defaults(fn=cmd_sync)

    sub.add_parser("reindex", help="re-embed everything (after changing the embedding model)").set_defaults(fn=cmd_reindex)

    s = sub.add_parser("summarize", help="(re)write the LLM summary of a document")
    s.add_argument("document")
    s.set_defaults(fn=cmd_summarize)

    s = sub.add_parser("search", help="retrieval only, no LLM")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--mode", choices=["hybrid", "keyword", "semantic"], default="hybrid")
    s.add_argument("-d", "--document", help="refs or title fragments, comma = any of")
    s.add_argument("-c", "--collection")
    s.add_argument("--kind")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("ask", help="one-shot question to the agent")
    s.add_argument("question")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("chat", help="interactive agent session")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("web", help="web chat UI")
    s.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    s.add_argument("--port", type=int, default=8120)
    s.set_defaults(fn=cmd_web)

    a = p.parse_args(argv)
    from . import settings
    settings.load()   # same saved model/settings as the web app
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
