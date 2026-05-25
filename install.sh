#!/usr/bin/env bash
# Install BrawlStar-Bot dependencies into ./venv.
# Handles the scrcpy-client / adbutils version conflict by installing
# scrcpy-client last with --no-deps.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "Creating venv (Python 3.12)…"
  /opt/homebrew/bin/python3.12 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "Upgrading pip / setuptools / wheel…"
pip install --upgrade pip setuptools wheel >/dev/null

echo "Installing main dependencies…"
pip install -r requirements.txt

echo "Installing scrcpy-client (v0.5.0 from git, no-deps)…"
pip install --no-deps "git+https://github.com/leng-yue/py-scrcpy-client.git@v0.5.0"

echo "Installing bsbot (editable)…"
pip install -e .

echo "Done. Activate with: source venv/bin/activate"
