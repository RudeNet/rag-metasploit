"""Runtime settings. Every value can be overridden with an environment variable."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name, default):
    return os.environ.get(name, default)


# Data
LIBRARY_DB = Path(_env("MSFRAG_LIBRARY_DB", ROOT / "library.db"))   # documents, chunks, FTS index, embeddings
LIBRARY_DIR = Path(_env("MSFRAG_LIBRARY_DIR", ROOT / "library"))    # files uploaded through the web UI / API
CHATS_DB = Path(_env("MSFRAG_CHATS_DB", ROOT / "chats.db"))         # chats, notes, API keys
SETTINGS_FILE = Path(_env("MSFRAG_SETTINGS", ROOT / "settings.json"))  # values changed in the UI

# Ollama (local only)
OLLAMA_HOST = _env("OLLAMA_HOST", "http://127.0.0.1:11434")
CHAT_MODEL = _env("MSFRAG_CHAT_MODEL", "qwen3:14b")
EMBED_MODEL = _env("MSFRAG_EMBED_MODEL", "nomic-embed-text")
# nomic-embed-text expects task prefixes; set both to "" for models that don't.
EMBED_DOC_PREFIX = _env("MSFRAG_EMBED_DOC_PREFIX", "search_document: ")
EMBED_QUERY_PREFIX = _env("MSFRAG_EMBED_QUERY_PREFIX", "search_query: ")

# Generation options passed to Ollama. num_gpu=999 offloads every layer to CUDA.
# 32k = qwen3's native window; measured on the 16 GB RTX 4060 Ti: qwen3:14b 13.4 GB + embeddings 0.3 GB, all on GPU.
NUM_CTX = int(_env("MSFRAG_NUM_CTX", "32768"))
NUM_GPU = int(_env("MSFRAG_NUM_GPU", "999"))
TEMPERATURE = float(_env("MSFRAG_TEMPERATURE", "0.2"))
THINK = _env("MSFRAG_THINK", "0") == "1"   # qwen3-style reasoning mode
KEEP_ALIVE = _env("MSFRAG_KEEP_ALIVE", "30m")

# Ingestion: documents are split into overlapping chunks; each chunk is embedded and full-text indexed.
CHUNK_CHARS = int(_env("MSFRAG_CHUNK_CHARS", "1500"))
CHUNK_OVERLAP = int(_env("MSFRAG_CHUNK_OVERLAP", "200"))
MAX_FILE_MB = int(_env("MSFRAG_MAX_FILE_MB", "200"))
AUTO_SUMMARY = _env("MSFRAG_AUTO_SUMMARY", "1") == "1"   # LLM-written summary of each new document

# Indexing / retrieval
EMBED_BATCH = int(_env("MSFRAG_EMBED_BATCH", "32"))
EMBED_MAX_CHARS = int(_env("MSFRAG_EMBED_MAX_CHARS", "2000"))
CANDIDATES = int(_env("MSFRAG_CANDIDATES", "60"))   # per retriever, before fusion
MAX_TOOL_ROUNDS = int(_env("MSFRAG_MAX_TOOL_ROUNDS", "8"))
# Answer length: steers the model and caps generated tokens (writing is the slow part on a 16 GB GPU).
ANSWER_LENGTH = _env("MSFRAG_ANSWER_LENGTH", "normal")
ANSWER_LENGTHS = {
    # level: (max tokens per model call, instruction)
    "concise": (400, "Answer length: CONCISE - every word costs time, so be terse. Lead with the direct answer in "
                     "one sentence, then at most 5 short bullets with citations. No introduction, no closing "
                     "summary, no generic advice. The user can ask for details."),
    "normal": (1200, "Answer length: NORMAL. Be focused: about 300 words, or a table plus a few bullets. Avoid "
                     "filler, repetition and generic advice."),
    "detailed": (-1, "Answer length: DETAILED. Thorough analysis is welcome when useful, but no filler."),
}


def answer_length():
    return ANSWER_LENGTHS.get(ANSWER_LENGTH, ANSWER_LENGTHS["normal"])


# External API (/api/v1, API-key protected); off until enabled in Settings.
API_ENABLED = _env("MSFRAG_API_ENABLED", "0") == "1"
# Tokens of NUM_CTX kept free for tool schemas (~1k) and the model's answer.
CONTEXT_RESERVE = int(_env("MSFRAG_CONTEXT_RESERVE", "3500"))


def ollama_options():
    return {"num_ctx": NUM_CTX, "num_gpu": NUM_GPU, "temperature": TEMPERATURE}
