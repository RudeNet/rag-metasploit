"""Local model management through Ollama: list, inspect, pull (download), delete, unload."""
import re
from functools import lru_cache

from . import config
from .llm import client

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*(:[A-Za-z0-9._\-]+)?$")

# Suggestions shown in the UI: tool-calling models that fit a 16 GB GPU (download size is shown while pulling).
SUGGESTED = ["qwen3:14b", "qwen3:8b", "qwen3:4b", "qwen2.5:14b", "qwen2.5:7b", "llama3.1:8b",
             "mistral-nemo:12b", "gpt-oss:20b", "granite3.3:8b"]


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or "")) and len(name) <= 120


def normalize(name: str) -> str:
    name = name.strip()
    return name if ":" in name.split("/")[-1] else f"{name}:latest"


@lru_cache(maxsize=64)
def _show(name, modified_at):
    """Cached per model version (modified_at changes when a tag is re-pulled)."""
    s = client().show(name)
    info = s.modelinfo or {}
    ctx = next((v for k, v in info.items() if k.endswith(".context_length")), None)
    return {"capabilities": list(getattr(s, "capabilities", None) or []), "context_length": ctx}


def installed():
    loaded = {m.model: (m.size_vram or 0, m.size or 0) for m in client().ps().models}
    out = []
    for m in client().list().models:
        d = m.details
        meta = _show(m.model, str(m.modified_at))
        vram, size = loaded.get(m.model, (0, 0))
        out.append({
            "name": m.model,
            "size_gb": round((m.size or 0) / 2**30, 1),
            "params": getattr(d, "parameter_size", None),
            "quant": getattr(d, "quantization_level", None),
            "family": getattr(d, "family", None),
            "capabilities": meta["capabilities"],
            "tools": "tools" in meta["capabilities"],
            "embedding": "embedding" in meta["capabilities"],
            "context_length": meta["context_length"],
            "loaded": m.model in loaded,
            "vram_pct": round(100 * vram / size) if size else None,
        })
    return sorted(out, key=lambda x: x["name"])


def find(name):
    name = normalize(name)
    return next((m for m in installed() if m["name"] == name), None)


def pull(name):
    """Yields progress dicts {status, completed, total} while Ollama downloads the model."""
    for p in client().pull(name, stream=True):
        yield {"status": p.status, "completed": p.completed or 0, "total": p.total or 0}


def delete(name):
    client().delete(name)
    _show.cache_clear()


def unload(name):
    """Free the model's VRAM now instead of waiting for keep_alive to expire."""
    try:
        client().generate(model=name, prompt="", keep_alive=0)
    except Exception:
        pass
