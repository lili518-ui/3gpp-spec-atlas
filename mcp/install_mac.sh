#!/bin/bash
# Atlas MCP: set up the Python environment on macOS and register the server with Claude Code.
# Uses a Python 3.11+ already on the Mac; otherwise installs uv (no Homebrew, no admin rights)
# and lets it fetch Python 3.12 into the user's home folder.
set -e
cd "$(dirname "$0")"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -d .venv ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  rm -rf .venv   # left over from an older Python
fi
if [ -n "$PY" ]; then
  echo "Using $($PY --version)"
  [ -d .venv ] || "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e ".[test]"
else
  echo "No Python 3.11+ found; using uv to get Python 3.12."
  UV="$(command -v uv || true)"
  [ -n "$UV" ] || UV="$HOME/.local/bin/uv"
  if [ ! -x "$UV" ]; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
  [ -d .venv ] || "$UV" venv --python 3.12 .venv
  "$UV" pip install -q --python .venv/bin/python -e ".[test]"
fi
echo "Python in .venv: $(.venv/bin/python --version)"
[ -f dist/atlas.db ] || .venv/bin/python -m atlas_core.build
.venv/bin/python -m atlas_core.validate | tail -3
# Local-scope entries belong to the folder claude runs in, so clear them for the home folder too.
for d in "$HOME" "$PWD"; do (cd "$d" && claude mcp remove atlas -s local >/dev/null 2>&1) || true; done
claude mcp remove atlas -s user >/dev/null 2>&1 || true
claude mcp add -s user atlas -- "$PWD/.venv/bin/atlas-mcp"
(cd "$HOME" && claude mcp list)
echo "Done. Start a new Claude Code session (claude) and ask where 'UL_CCCH / RRC Setup Req' is defined."
