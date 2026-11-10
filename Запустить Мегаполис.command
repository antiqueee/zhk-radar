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
uv run --frozen radar serve &
radar_pid=$!
trap 'kill "$radar_pid" 2>/dev/null || true' EXIT INT TERM
for attempt in {1..40}; do
  if curl -fsS --max-time 1 http://127.0.0.1:8765/api/settings >/dev/null 2>&1; then
    open http://127.0.0.1:8765
    break
  fi
  if ! kill -0 "$radar_pid" 2>/dev/null; then
    wait "$radar_pid"
    exit 1
  fi
  sleep 0.25
done
wait "$radar_pid"
