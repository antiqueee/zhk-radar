#!/bin/zsh
set -eu
cd -- "${0:A:h}"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  print 'Для запуска нужен uv: https://docs.astral.sh/uv/getting-started/installation/'
  read '?Нажмите Enter для выхода'
  exit 1
fi
if curl -fsS --max-time 1 http://127.0.0.1:8765/api/settings >/dev/null 2>&1; then
  open http://127.0.0.1:8765
  exit 0
fi
if [[ ! -x .venv/bin/radar ]]; then
  uv sync --frozen
fi
.venv/bin/python scripts/local_service.py start
open http://127.0.0.1:8765
