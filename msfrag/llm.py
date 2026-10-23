"""Thin wrapper around the local Ollama server."""
from functools import lru_cache

import ollama

from . import config


@lru_cache(maxsize=1)
def client():
    return ollama.Client(host=config.OLLAMA_HOST)


def embed(texts):
    resp = client().embed(model=config.EMBED_MODEL, input=texts, truncate=True,
                          keep_alive=config.KEEP_ALIVE, options={"num_gpu": config.NUM_GPU})
    return resp["embeddings"]


def embed_query(text):
    return embed([config.EMBED_QUERY_PREFIX + text])[0]


def chat(messages, tools=None, stream=False):
    options = config.ollama_options()
    max_tokens = config.answer_length()[0]
    if max_tokens > 0:
        # agent answers only (summaries use their own call); reasoning tokens count too, so leave room for them
        options["num_predict"] = max_tokens + (3000 if config.THINK else 0)
    kwargs = dict(model=config.CHAT_MODEL, messages=messages, tools=tools, stream=stream,
                  keep_alive=config.KEEP_ALIVE, options=options)
    kwargs["think"] = config.THINK   # explicit: qwen3 otherwise reasons silently by default
    return client().chat(**kwargs)
