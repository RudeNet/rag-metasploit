"""Conversation context management.

The full transcript is kept for storage and the UI, but what is sent to the model must fit in NUM_CTX.
Two mechanisms keep follow-up questions answerable as the chat grows:

* DocMemory - every document touched in the chat (ref, title, pages/sections seen), injected into the system
  prompt, so "that contract" / "the second report" still resolve after old turns are trimmed.
* build_view - compacts old tool results to one-line digests, then drops the oldest turns (keeping their
  questions as a topic list), never touching the current turn's user message.
"""
import json
import re
from collections import OrderedDict

from . import config

CHARS_PER_TOKEN = 3.0      # conservative for JSON-heavy content
NOTE_DOC_RE = re.compile(r"^(?:[-*]\s*|\|\s*)\**\[?(D\d+)\]?\**\s*[:|]?(.*)$", re.I)
MEMORY_LIMIT = 80          # documents listed in the memory block (most recent)


def estimate_tokens(messages):
    total = 0
    for m in messages:
        total += len(m.get("content") or "") + 16
        for tc in m.get("tool_calls") or []:
            total += len(json.dumps(tc.get("function", {}), default=str))
    return int(total / CHARS_PER_TOKEN)


def _parse(content):
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return None


class DocMemory:
    def __init__(self):
        self.items: OrderedDict[str, dict] = OrderedDict()   # ref -> {title, pages:set, note}
        self.from_notes: set[str] = set()   # known via an attached note (already in the prompt there)

    def _touch(self, ref, title=None, page=None, note=None):
        item = self.items.pop(ref, None) or {"title": title or "?", "pages": set(), "note": ""}
        if title:
            item["title"] = title
        if page:
            item["pages"].add(int(page))
        if note:
            item["note"] = note
        self.items[ref] = item   # re-insert so most recently seen ends last
        self.from_notes.discard(ref)

    def seed_from_notes(self, notes):
        """Count documents listed in attached notes as remembered (lines "- D12: ..." or table rows)."""
        for n in notes:
            for line in (n.get("content") or "").splitlines():
                m = NOTE_DOC_RE.match(line.strip())
                if m and m.group(1).upper() not in self.items:
                    self.items[m.group(1).upper()] = {"title": (m.group(2) or "").strip(" |")[:120],
                                                      "pages": set(), "note": ""}
                    self.from_notes.add(m.group(1).upper())

    def update(self, tool_name, result):
        if isinstance(result, str):
            result = _parse(result)
        if tool_name == "search_documents" and isinstance(result, dict):
            result = result.get("results")   # {note, results} when filters were relaxed
        if tool_name == "search_documents" and isinstance(result, list):
            for h in result:
                self._touch(h["ref"], h.get("title"), h.get("page"))
        elif tool_name == "list_documents" and isinstance(result, list):
            for d in result:
                self._touch(d["ref"], d.get("title"))
        elif tool_name in ("document_info", "read_document", "scan_document") and isinstance(result, dict) \
                and result.get("ref"):
            note = {"document_info": "details fetched", "read_document": "text read",
                    "scan_document": "fully scanned"}[tool_name]
            self._touch(result["ref"], result.get("title"), note=note)
            chunks = result.get("chunks")
            # read_document returns a list of chunk dicts; document_info's "chunks" is an int count
            if isinstance(chunks, list):
                for c in chunks:
                    if isinstance(c, dict) and c.get("page"):
                        self.items[result["ref"]]["pages"].add(int(c["page"]))

    def rebuild(self, messages, clear=True):
        if clear:
            self.items.clear()
            self.from_notes.clear()
        for m in messages:
            if m.get("role") == "tool":
                self.update(m.get("tool_name"), m.get("content"))

    @staticmethod
    def line(item):
        pages = sorted(item["pages"])
        bits = [item["title"]]
        if pages:
            bits.append("pages seen " + ", ".join(map(str, pages[:15])) + ("…" if len(pages) > 15 else ""))
        if item["note"]:
            bits.append(item["note"])
        return " | ".join(bits)

    def render(self):
        recent = [(r, i) for r, i in self.items.items() if r not in self.from_notes][-MEMORY_LIMIT:]
        if not recent:
            return ""
        lines = "\n".join(f"- {ref}: {self.line(i)}" for ref, i in recent)
        return ("\n\nDocuments already used in this conversation (oldest first). Use this to resolve follow-up "
                "references like 'that report' or 'the second one'; search or read again when you need the text:\n"
                + lines)


def digest(tool_name, content):
    """One-line summary of a tool result, used once the full result no longer fits."""
    r = _parse(content)
    if tool_name == "search_documents" and isinstance(r, dict) and isinstance(r.get("results"), list):
        r = r["results"]
    if tool_name == "search_documents" and isinstance(r, list):
        refs = "; ".join(f"{h['ref']}{' p.' + str(h['page']) if h.get('page') else ''}: {(h.get('text') or '')[:60]}"
                         for h in r)
        return f"[compacted] {len(r)} passages: {refs}"
    if tool_name == "list_documents" and isinstance(r, list):
        return "[compacted] " + ", ".join(f"{d['ref']} {d.get('title')}" for d in r[:30])
    if tool_name in ("document_info", "read_document", "scan_document") and isinstance(r, dict) and r.get("ref"):
        extra = (r.get("summary") or "")[:200] if tool_name == "document_info" else ""
        return f"[compacted] {tool_name} {r['ref']} {r.get('title') or ''} {extra}".strip()
    return "[compacted] " + (content or "")[:300]


def _turns(messages):
    """Split non-system messages into turns, each starting with a user message."""
    turns = []
    for m in messages:
        if m.get("role") == "system":
            continue
        if m.get("role") == "user" or not turns:
            turns.append([])
        turns[-1].append(m)
    return turns


def build_view(messages, system, memory: DocMemory, budget=None):
    """Return (messages_for_model, info) fitting within `budget` tokens."""
    budget = budget or (config.NUM_CTX - config.CONTEXT_RESERVE)
    turns = [[{k: v for k, v in m.items() if k != "stats"} for m in t] for t in _turns(messages)]
    dropped = []
    compacted = 0

    def assemble():
        sys_text = system + memory.render()
        if dropped:
            sys_text += ("\n\nEarlier questions in this conversation (full exchanges trimmed to save space):\n"
                         + "\n".join(f"- {q[:160]}" for q in dropped))
        return [{"role": "system", "content": sys_text}] + [m for t in turns for m in t]

    view = assemble()
    # 1) compact tool results, oldest turn first; the current (last) turn last of all
    for t in turns:
        if estimate_tokens(view) <= budget:
            break
        tool_msgs = [m for m in t if m["role"] == "tool" and not m["content"].startswith("[compacted]")]
        if t is turns[-1]:
            tool_msgs = tool_msgs[:-1]   # keep the freshest result of the running turn intact
        for m in tool_msgs:
            m["content"] = digest(m.get("tool_name"), m["content"])
            compacted += 1
        view = assemble()
    # 2) drop whole old turns, remembering their questions
    while estimate_tokens(view) > budget and len(turns) > 1:
        old = turns.pop(0)
        if old and old[0]["role"] == "user":
            dropped.append(old[0]["content"])
        view = assemble()

    info = {"estimated_tokens": estimate_tokens(view), "limit": config.NUM_CTX, "budget": budget,
            "compacted_results": compacted, "dropped_turns": len(dropped), "remembered_docs": len(memory.items)}
    return view, info
