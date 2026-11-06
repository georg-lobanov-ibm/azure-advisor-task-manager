#!/usr/bin/env bash
# Create .venv and install requirements-dev.txt (requirements.txt plus pytest)
# into it.
#
# Run automatically by the "Setup virtualenv and install dependencies" task
# (.vscode/tasks.json) before every launch configuration in
# .vscode/launch.json. Safe to run repeatedly: the venv is only created if
# missing, and dependencies are only reinstalled when requirements.txt or
# requirements-dev.txt is newer than the last successful install.
#
# Uses uv if it's on PATH (SETUP.md lists it as a prerequisite for the Jira
# MCP server), otherwise falls back to python3's built-in venv + pip.

set -euo pipefail

cd "$(dirname "$0")/.."

VENV=.venv
STAMP="$VENV/.requirements.stamp"

fail() {
    echo "error: $*" >&2
    exit 1
}

if command -v uv >/dev/null 2>&1; then
    [ -x "$VENV/bin/python" ] || uv venv "$VENV"
    install_requirements() { uv pip install --python "$VENV/bin/python" -r requirements-dev.txt; }
else
    if [ ! -x "$VENV/bin/python" ]; then
        if ! python3 -m venv "$VENV"; then
            # A failed `python3 -m venv` can leave a half-built venv behind
            # (python present, pip missing); remove it so the next run starts clean.
            rm -rf "$VENV"
            fail "could not create $VENV with 'python3 -m venv' (on Debian/Ubuntu this needs the python3-venv package). Either install uv (https://docs.astral.sh/uv/, also needed for the Jira MCP server) or run: sudo apt install python3-venv"
        fi
    fi
    if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
        fail "$VENV has no pip (was it created by uv?). Install uv so it is on PATH, or delete $VENV and re-run."
    fi
    install_requirements() { "$VENV/bin/python" -m pip install -r requirements-dev.txt; }
fi

if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ] || [ requirements-dev.txt -nt "$STAMP" ]; then
    install_requirements
    touch "$STAMP"
fi
