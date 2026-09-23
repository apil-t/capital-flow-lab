#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
command -v uv >/dev/null || { echo "uv not found: https://docs.astral.sh/uv/getting-started/installation/" >&2; exit 1; }
[ -d .venv ] || uv venv --python '>=3.11' .venv
uv pip install --python .venv -e '.[pilot]'
echo "Done. Activate with: source .venv/bin/activate"
