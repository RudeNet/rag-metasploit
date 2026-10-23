"""Tool-calling agent loop running on a local Ollama chat model."""
import contextlib
import json
import threading
import time
from datetime import date

from . import config
from .analyze import scan_document
from .context import DocMemory, build_view
from .library import Filters
from .llm import chat
from .notes import notes_block
from .retriever import Retriever

_FILTER_PROPS = {
    "document": {"type": "string", "description": "Restrict to documents: refs like 'D3' or file/title words, "
                 "comma = any of, e.g. 'D3, D7' or 'contract'"},
    "collection": {"type": "string", "description": "Collection name (see list_documents)"},
    "kind": {"type": "string", "description": "File type: pdf, docx, pptx, xlsx, markdown, text, html, csv, email, ..."},
}


def _tool(name, description, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


TOOLS = [
    _tool("search_documents",
          "Hybrid semantic + keyword search over the Metasploit Framework API reference (one document per Ruby "
          "class/module/mixin). Use for any question about classes, methods, mixins, options or how to write a "
          "module. Returns passages with their document ref (D<id>) and section (e.g. a method signature).",
          {"query": {"type": "string", "description": "Natural-language or keyword query, e.g. "
                     "'register_options datastore', 'Msf::Exploit::Remote::HttpClient send_request_cgi', "
                     "'report_vuln', 'Rex::Socket::Tcp'"},
           "limit": {"type": "integer", "description": "Passages to return (default 8, max 25)"},
           **_FILTER_PROPS}, ["query"]),
    _tool("list_documents",
          "List API reference pages (classes/modules/mixins) with their title, namespace (collection) and a short "
          "summary. Use to see what classes exist, to find a class by name, or to browse a namespace.",
          {"query": {"type": "string", "description": "Words in the class name or summary, e.g. 'HttpClient', "
                     "'Kerberos', 'SMB', 'auxiliary scanner' (optional)"},
           "limit": {"type": "integer", "description": "Max documents (default 30)"},
           **_FILTER_PROPS}),
    _tool("document_info",
          "Summary and section outline (method list) of one API reference page.",
          {"document": {"type": "string", "description": "Ref like D3, or a class-name fragment like 'HttpClient'"}},
          ["document"]),
    _tool("read_document",
          "Read one API reference page in order: its class description, included mixins, then each method with its "
          "signature and source. Use to study a class fully; call again with the returned next_chunk to continue.",
          {"document": {"type": "string", "description": "Ref like D3"},
           "page": {"type": "integer", "description": "(rarely needed; these pages are single-page)"},
           "from_chunk": {"type": "integer", "description": "Start at this chunk number (default 0)"},
           "chunks": {"type": "integer", "description": "How many chunks (default 6, max 12)"}}, ["document"]),
    _tool("scan_document",
          "Read an ENTIRE API reference page with a question and return every relevant finding. Slower (several "
          "model calls) but complete: use when you need all of a large class, e.g. 'list every method HttpClient "
          "provides and what each does', 'all datastore options this mixin registers', 'every callback a module "
          "must implement'.",
          {"document": {"type": "string", "description": "Ref like D3"},
           "question": {"type": "string", "description": "What to extract or analyze"}}, ["document", "question"]),
]


CATALOG_LIMIT = 40   # documents listed in the system prompt (~25 tokens each); more -> list_documents


def catalog(db):
    """A few top namespaces, so the model knows which collections it can filter by without guessing."""
    cols = db.collections()[:CATALOG_LIMIT]
    lines = [f"- {c['collection'] or '(top level)'}: {c['documents']} classes" for c in cols if c["documents"]]
    return ("\n\nNamespaces available as the `collection` filter (largest first):\n" + "\n".join(lines)) if lines else \
        "\n\nThe reference is empty: ingest the scraped docs with `python -m msfrag add metasploit-api-docs`."


def system_prompt(db):
    st = db.stats()
    return f"""You are a Metasploit Framework module-development assistant. You help the user understand the \
Framework's Ruby API and write correct modules (exploits, auxiliary, post, payloads) and library code, grounded \
in a local copy of the official API reference ({st['ready']:,} classes/modules/mixins indexed; \
{st['chunks']:,} passages). Today is {date.today().isoformat()}.

Each "document" (D<id>) is one API reference page for a Ruby class, module or mixin (e.g. \
`Msf::Exploit::Remote::HttpClient`), with its description, the mixins it includes, and its methods with \
signatures and source. Collections are the top-level namespaces (Msf, Rex, Msf::Exploit, ...).

How to answer:
- Ground every claim about the API in tool results from THIS conversation - real class names, method names, \
signatures and arguments. Never invent a method, option, mixin or signature. If you are unsure a method exists, \
search or read for it first.
- Cite the reference pages you used inline as [D<id>] (e.g. [D12]) right where you rely on them. Only cite refs \
returned by the tools.
- Workflow: use search_documents to find the right class/method/mixin; list_documents to discover classes or \
browse a namespace; document_info for a class's method outline; read_document to study a class in order; \
scan_document when you need all of a large class.
- Use the collection (namespace) filter only with namespaces listed below or returned by tools; never guess refs \
or collections.

Writing code (do this whenever the user asks how to do something, or a short example makes the answer concrete):
- Give idiomatic, runnable Ruby in ```ruby fenced blocks, following Metasploit conventions: the right superclass \
(`Msf::Exploit::Remote`, `Msf::Auxiliary`, `Msf::Post`, ...), `Msf::Exploit::Remote::` mixins via `include`, an \
`initialize` with `super(update_info(info, ...))` setting Name/Description/Author/References/etc., \
`register_options([...])`, and the expected entry method (`exploit`, `run`, `check`, `run_host`, ...).
- Use only methods, options and mixins you have confirmed in the reference, and name the page you took each from \
(a `# from HttpClient [D12]` comment or a line after the block). Prefer the framework's own helpers \
(`send_request_cgi`, `connect`/`sock`, `report_vuln`, `store_loot`, `print_good`/`print_status`, `fail_with`, \
`datastore[...]`) over reinventing them.
- Keep examples minimal and focused on the question; say what is illustrative vs. required, and flag anything you \
could not verify in the reference.
- Explain the key methods briefly around the code so the user learns the API, not just the snippet.
- If the reference does not cover something, say so plainly; you may add general Metasploit knowledge when you \
clearly label it as not coming from the indexed reference.""" + catalog(db)


class Agent:
    def __init__(self, on_event=None, retriever=None, history=None, notes=None):
        self.retriever = retriever or Retriever()
        self.db = self.retriever.db
        self.on_event = on_event or (lambda kind, data: None)
        self.cancel = threading.Event()
        self.lock = contextlib.nullcontext()   # the web app shares one database connection: it sets a real lock
        self.notes = list(notes or [])
        self.reset()
        if history:  # restore a saved conversation; the system prompt is always rebuilt fresh
            self.messages = [m for m in history if m.get("role") != "system"]
            self.memory.rebuild(self.messages, clear=False)
        # computed even for an empty chat, so attached notes show up in the context meter
        self.context_info = build_view(self.messages, self.system, self.memory)[1]

    @property
    def system(self):
        # rebuilt per call: the library grows while chats are open
        with self.lock:
            return system_prompt(self.db) + notes_block(self.notes)

    def reset(self):
        # Full transcript (no system message). What the model sees is built per call by build_view().
        self.messages = []
        self.memory = DocMemory()
        self.memory.seed_from_notes(self.notes)
        self.context_info = {}

    # ---- tools ---------------------------------------------------------
    def _resolve(self, ref):
        doc = self.db.get_document(ref)
        if doc is None:
            raise LookupError(f"no document matches {ref!r} - use list_documents to see refs")
        return doc

    def run_tool(self, name, args):
        args = args or {}
        try:
            if name == "scan_document":   # long-running: hold the lock only while touching the shared connection
                with self.lock:
                    doc = self._resolve(args["document"])
                return scan_document(doc["id"], args["question"],
                                     lambda i, n: self.on_event("progress", f"scanning {doc['ref']}: part {i}/{n}"))
            with self.lock:
                return self._run_tool(name, args)
        except Exception as e:  # report back to the model so it can correct itself
            return {"error": f"{type(e).__name__}: {e}"}

    def _run_tool(self, name, args):
        if name == "search_documents":
            limit = max(4, min(int(args.get("limit") or 8), 25))   # models like limit=1, which starves the answer
            f = Filters.from_args(args)
            hits = self.retriever.search(args.get("query", ""), f, limit)
            if not hits and f:
                # a guessed collection/document filter is the usual cause: retry across the whole library
                hits = self.retriever.search(args.get("query", ""), Filters(), limit)
                if hits:
                    return {"note": f"No matches with filters {f.__dict__}; these results are from all documents.",
                            "results": hits}
            return hits or {"results": [], "hint": "No matches. Rephrase, use key terms, or drop filters."}
        if name == "list_documents":
            docs = self.db.list_documents(args.get("query"), Filters.from_args(args),
                                          max(1, min(int(args.get("limit") or 30), 100)))
            if not docs and args.get("query"):   # the words may not be in titles - list everything instead
                docs = self.db.list_documents(None, Filters.from_args(args), 30)
            return docs or {"results": [], "hint": "The library is empty or nothing matches the filters."}
        if name == "document_info":
            return self._resolve(args["document"])
        if name == "read_document":
            doc = self._resolve(args["document"])
            r = self.db.read(doc["id"], int(args.get("from_chunk") or 0), min(int(args.get("chunks") or 6), 12),
                             int(args["page"]) if args.get("page") not in (None, "") else None)
            return {"ref": doc["ref"], "title": doc["title"], **r}
        return {"error": f"unknown tool {name}"}

    # ---- loop ----------------------------------------------------------
    def ask(self, question):
        self.cancel.clear()
        self._stats = {"output_tokens": 0, "gen_ns": 0, "model_calls": 0, "started": time.time()}
        self.messages.append({"role": "user", "content": question})
        for _ in range(config.MAX_TOOL_ROUNDS):
            content, calls = self._stream_turn()
            if self.cancel.is_set():
                self.messages.append({"role": "assistant", "content": content + " [stopped]",
                                      "stats": self._finish_stats()})
                return content
            msg = {"role": "assistant", "content": content}
            if calls:
                msg["tool_calls"] = calls
            else:
                msg["stats"] = self._finish_stats()
            self.messages.append(msg)
            if not calls:
                return content
            for call in calls:
                fn = call["function"]
                self.on_event("tool_call", fn)
                result = self.run_tool(fn["name"], fn.get("arguments"))
                self.memory.update(fn["name"], result)
                text = json.dumps(result, ensure_ascii=False, default=str)
                if len(text) > 16000:
                    text = text[:16000] + "...(truncated)"
                self.on_event("tool_result", {"name": fn["name"], "result": result})
                self.messages.append({"role": "tool", "content": text, "tool_name": fn["name"]})
        self.on_event("warning", "tool round limit reached")
        self.messages[-1]["stats"] = self._finish_stats()
        return content

    def _finish_stats(self):
        st = self._stats
        stats = {"output_tokens": st["output_tokens"], "model_calls": st["model_calls"],
                 "seconds": round(time.time() - st["started"], 1),
                 "tokens_per_s": round(st["output_tokens"] / (st["gen_ns"] / 1e9), 1) if st["gen_ns"] else None,
                 "context_tokens": self.context_info.get("prompt_tokens") or self.context_info.get("estimated_tokens"),
                 "model": config.CHAT_MODEL}
        self.on_event("stats", stats)
        return stats

    def _stream_turn(self):
        content, calls = [], []
        self.on_event("turn_start", None)
        # length instruction is read per call, so changing the setting applies to running chats immediately
        system = self.system + "\n- " + config.answer_length()[1]
        view, info = build_view(self.messages, system, self.memory)
        for chunk in chat(view, tools=TOOLS, stream=True):
            if self.cancel.is_set():
                break
            if chunk.done and getattr(chunk, "done_reason", None) == "length":
                note = ("\n\n_[Answer cut at the length limit - set Answer length to Normal or Detailed, "
                        "or ask to continue.]_")
                content.append(note)
                self.on_event("token", note)
            if chunk.done:
                if chunk.prompt_eval_count:
                    info["prompt_tokens"] = chunk.prompt_eval_count
                self._stats["output_tokens"] += chunk.eval_count or 0
                self._stats["gen_ns"] += chunk.eval_duration or 0
                self._stats["model_calls"] += 1
            msg = chunk.message
            if getattr(msg, "thinking", None):
                self.on_event("thinking", msg.thinking)
            if msg.content:
                content.append(msg.content)
                self.on_event("token", msg.content)
            for tc in msg.tool_calls or []:
                calls.append({"function": {"name": tc.function.name,
                                           "arguments": dict(tc.function.arguments or {})}})
        self.context_info = info
        self.on_event("context", info)
        self.on_event("turn_end", None)
        return "".join(content), calls
