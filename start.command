#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ -x "$HOME/.local/bin/uv" ]]; then
  exec "$HOME/.local/bin/uv" run --locked sub2easy --data-dir "$PWD/data"
elif command -v uv >/dev/null; then
  exec uv run --locked sub2easy --data-dir "$PWD/data"
elif [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m sub2easy.gui --data-dir "$PWD/data"
else
  echo '请先安装 uv，或执行 python3 -m venv .venv && .venv/bin/pip install -e .'
  read -k 1
fi
