#!/bin/bash
# Kemi — double-click this file to start.
# (macOS/Linux: double-click runs it; if it opens in an editor instead,
#  right-click → Open With → Terminal, or run: bash Kemi-Start.command)
cd "$(dirname "$0")/.."
echo "Starting Kemi… a browser window will open in a moment."
if command -v kemi >/dev/null 2>&1; then
    exec kemi app
elif command -v python3 >/dev/null 2>&1; then
    exec python3 -m kemi app
else
    echo "Python 3.10+ is required. Install it from https://www.python.org/downloads/ and try again."
    read -r -p "Press Enter to close."
fi
