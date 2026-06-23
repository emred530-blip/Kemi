#!/usr/bin/env sh
# Build a standalone Kemi desktop app for the current OS.
#   sh packaging/build.sh
# Produces dist/Kemi (macOS: dist/Kemi.app, Windows: dist\Kemi.exe).
set -eu
cd "$(dirname "$0")/.."

python3 -m pip install --quiet --upgrade pyinstaller pynacl
python3 -m pip install --quiet -e .
python3 -m PyInstaller --clean --noconfirm packaging/kemi.spec

echo ""
echo "Built standalone app in ./dist/"
ls -la dist/ 2>/dev/null || true
echo ""
echo "macOS: open dist/Kemi.app   |   Windows: dist\\Kemi.exe   |   Linux: ./dist/Kemi"
echo "Distribute that file — end users need no Python and no terminal."
