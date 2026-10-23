"""LLM analysis of whole documents.

* summarize_document - a short Markdown brief stored with the document (shown in the library and given to the agent).
* scan_document - map step over the full text for one question, so the agent can analyze documents much larger
  than its context window (e.g. "list every obligation of the supplier in D4").
Both read the document in parts of PART_CHARS, so any size works; very long documents are sampled evenly.
"""
from . import config
from .library import connect
from .llm import client

PART_CHARS = 18000       # ~5-6k tokens per model call, fits easily next to the prompt in an 8k+ window
MAX_PARTS = 12           # longer documents: evenly spaced parts (the result says so)

SUMMARY_PROMPT = """You summarize one Metasploit Framework API reference page (a Ruby class, module or mixin) \
for a developer index. Write Markdown, max ~220 words: one sentence saying what the class/module/mixin is and \
what a module author uses it for, then "**Key methods**" with 3-7 bullets naming the most useful methods and \
what each does, then "**Mixins / includes**": the notable modules it includes or the superclass, in one line. \
Use exact Ruby names from the text. Only facts from the text, no preamble."""

PART_PROMPT = """You read one part of a longer API reference page and write compact notes (max ~150 words of \
bullets) of its classes, methods (with signatures), mixins and important behaviour. Keep exact Ruby names. \
No preamble."""

SCAN_PROMPT = """You read one part of a Metasploit API reference page and extract everything relevant to the \
user's question. Write bullets naming the exact Ruby methods, signatures, options, mixins or constants involved, \
e.g. "- send_request_cgi('uri'=>..., 'method'=>...) returns a Response". Quote exact names and argument shapes \
where it matters. If nothing in this part is relevant, answer exactly: NOTHING RELEVANT."""


def _call(system, user, max_tokens):
    resp = client().chat(model=config.CHAT_MODEL, think=False, keep_alive=config.KEEP_ALIVE,
                         options={**config.ollama_options(), "temperature": 0.1, "num_predict": max_tokens},
                         messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return (resp.message.content or "").strip()


def _parts(chunks, paged=True):
    """Group (page, heading, text) chunks into ~PART_CHARS texts with page markers -> [(first_page, last_page, text)]."""
    parts, buf, size, first, last, page_seen = [], [], 0, None, None, None
    for page, _heading, text in chunks:
        if buf and size + len(text) > PART_CHARS:
            parts.append((first, last, "\n\n".join(buf)))
            buf, size, first, page_seen = [], 0, None, None
        if paged and page != page_seen:
            buf.append(f"[p.{page}]")
            page_seen = page
        buf.append(text)
        size += len(text)
        first = page if first is None else first
        last = page
    if buf:
        parts.append((first, last, "\n\n".join(buf)))
    sampled = False
    if len(parts) > MAX_PARTS:
        step = len(parts) / MAX_PARTS
        parts = [parts[int(i * step)] for i in range(MAX_PARTS)]
        sampled = True
    return parts, sampled


def _doc(con, doc_id):
    d = con.execute("SELECT id, title, name, kind, pages FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if d is None:
        raise ValueError(f"D{doc_id} not found")
    chunks = [(r[0], r[1], r[2]) for r in con.execute(
        "SELECT page, heading, text FROM chunks WHERE doc_id = ? ORDER BY seq", (doc_id,))]
    return d, chunks


def summarize_document(doc_id):
    con = connect()
    try:
        d, chunks = _doc(con, doc_id)
        if not chunks:
            return None
        header = f"Document title: {d['title']} | file: {d['name']} | type: {d['kind']} | pages: {d['pages']}"
        parts, sampled = _parts(chunks, (d["pages"] or 0) > 1)
        if len(parts) == 1:
            summary = _call(SUMMARY_PROMPT, f"{header}\n\n{parts[0][2]}", 600)
        else:
            notes = [f"Pages {a}-{b}:\n" + _call(PART_PROMPT, f"{header}\n\n{text}", 400) for a, b, text in parts]
            summary = _call(SUMMARY_PROMPT, f"{header}\nNotes written while reading the document in parts"
                            f"{' (evenly sampled, not every page)' if sampled else ''}:\n\n" + "\n\n".join(notes), 600)
        lines = summary.splitlines()
        while lines and (lines[0].startswith("#") or lines[0].lower().startswith("document title:") or not lines[0].strip()):
            lines.pop(0)   # models sometimes echo the header
        summary = "\n".join(lines).strip() or summary
        con.execute("UPDATE documents SET summary = ? WHERE id = ?", (summary, doc_id))
        con.commit()
        return summary
    finally:
        con.close()


def scan_document(doc_id, question, on_progress=None):
    """Returns {document, parts_read, sampled, findings: [{pages, notes}]} for the agent to answer from."""
    con = connect(readonly=True)
    try:
        d, chunks = _doc(con, doc_id)
    finally:
        con.close()
    paged = (d["pages"] or 0) > 1
    parts, sampled = _parts(chunks, paged)
    findings = []
    for i, (a, b, text) in enumerate(parts, 1):
        if on_progress:
            on_progress(i, len(parts))
        where = f" (pages {a}-{b})" if paged else " (no page numbers)"
        notes = _call(SCAN_PROMPT, f"Question: {question}\n\nDocument: {d['title']} - part {i} of {len(parts)}"
                      f"{where}\n\n{text}", 700)
        if notes and "NOTHING RELEVANT" not in notes.upper()[:40]:
            findings.append({"pages": (f"{a}-{b}" if a != b else str(a)) if paged else None, "notes": notes})
    return {"ref": f"D{d['id']}", "title": d["title"], "parts_read": len(parts),
            "sampled": sampled, "findings": findings or "nothing relevant found in the document"}
