#!/usr/bin/env bash
# One-time setup for Metasploit API Explorer on Linux / macOS:
# Python venv + packages, Ollama models.
#
#   ./scripts/setup.sh                      # everything
#   CHAT_MODEL=qwen3:8b ./scripts/setup.sh  # different chat model
#   SKIP_MODELS=1 ./scripts/setup.sh        # code/packages only (e.g. after git pull)
set -euo pipefail
cd "$(dirname "$0")/.."

CHAT_MODEL="${CHAT_MODEL:-qwen3:14b}"
EMBED_MODEL="${EMBED_MODEL:-nomic-embed-text}"
step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }

step "Checking Python (3.10 or newer)"
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "Python 3.10+ is required (e.g. sudo apt install python3 python3-venv)"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python $("$PY" -V) found, but 3.10 or newer is required"; exit 1; }
"$PY" -V

step "Creating virtual environment (.venv) and installing packages"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt --quiet
echo "Packages installed"

step "Checking Ollama"
if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama is not installed. Install it with:"
  echo "    curl -fsSL https://ollama.com/install.sh | sh        (Linux)"
  echo "    or download from https://ollama.com/download          (macOS)"
  exit 1
fi
if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null; then
  echo "Starting the Ollama server..."
  (ollama serve >/dev/null 2>&1 &) ; sleep 5
fi

if [ -z "${SKIP_MODELS:-}" ]; then
  step "Pulling models: $CHAT_MODEL (chat) and $EMBED_MODEL (API-page embeddings)"
  ollama pull "$CHAT_MODEL"
  ollama pull "$EMBED_MODEL"
  if [ "$CHAT_MODEL" != "qwen3:14b" ]; then
    printf '{\n  "chat_model": "%s"\n}\n' "$CHAT_MODEL" > settings.json
  fi
fi

step "Checking the installation"
.venv/bin/python -m msfrag doctor || true
printf '\n\033[32mSetup complete. Next:\n  .venv/bin/python scripts/scrape_api_docs.py\n  .venv/bin/python scripts/ingest_api_docs.py metasploit-api-docs --no-summary\n  ./scripts/start.sh   (then open http://127.0.0.1:8120)\033[0m\n'
