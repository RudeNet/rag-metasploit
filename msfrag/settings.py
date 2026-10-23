"""Runtime-adjustable model settings, persisted to settings.json and applied onto `config`.

Agents read config at call time, so changes take effect on the next model call without a restart.
"""
import json

from . import config

PATH = config.SETTINGS_FILE

# name -> (config attribute, type, validator)
FIELDS = {
    "chat_model": ("CHAT_MODEL", str, lambda v: bool(v.strip())),
    "temperature": ("TEMPERATURE", float, lambda v: 0.0 <= v <= 2.0),
    "num_ctx": ("NUM_CTX", int, lambda v: v in CONTEXT_CHOICES),
    "think": ("THINK", bool, lambda v: True),
    "max_tool_rounds": ("MAX_TOOL_ROUNDS", int, lambda v: 1 <= v <= 20),
    "answer_length": ("ANSWER_LENGTH", str, lambda v: v in config.ANSWER_LENGTHS),
    "api_enabled": ("API_ENABLED", bool, lambda v: True),   # external /api/v1 access (API key required)
    "auto_summary": ("AUTO_SUMMARY", bool, lambda v: True),  # LLM summary of each newly added document
}
CONTEXT_CHOICES = [8192, 16384, 24576, 32768]


def current():
    return {name: getattr(config, attr) for name, (attr, _, _) in FIELDS.items()}


def apply(values: dict):
    """Validate and apply a partial update; raises ValueError on bad input."""
    clean = {}
    for name, raw in values.items():
        if name not in FIELDS:
            raise ValueError(f"unknown setting {name!r}")
        attr, typ, ok = FIELDS[name]
        try:
            v = typ(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: expected {typ.__name__}") from None
        if not ok(v):
            raise ValueError(f"{name}: value {raw!r} out of range")
        clean[name] = v
    for name, v in clean.items():
        setattr(config, FIELDS[name][0], v)
    return clean


def load():
    if PATH.exists():
        try:
            apply(json.loads(PATH.read_text(encoding="utf-8")))
        except (ValueError, OSError) as e:
            print(f"ignoring invalid {PATH.name}: {e}")


def save():
    PATH.write_text(json.dumps(current(), indent=2), encoding="utf-8")
