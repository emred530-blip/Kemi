#!/usr/bin/env sh
# Kemi one-line installer:
#   curl -fsSL https://raw.githubusercontent.com/emred530-blip/Kemi/main/scripts/install.sh | sh
#
# Creates an isolated venv at ~/.kemi/venv, installs Kemi into it and links
# the `kemi` command into ~/.local/bin. Safe to re-run to upgrade.
set -eu

REPO="${KEMI_REPO:-https://github.com/emred530-blip/Kemi}"
BRANCH="${KEMI_BRANCH:-main}"
HOME_DIR="${HOME:?}"
VENV="$HOME_DIR/.kemi/venv"
BIN_DIR="$HOME_DIR/.local/bin"

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PY="$(find_python)" || {
    echo "error: Python 3.10+ not found." >&2
    echo "  macOS:  brew install python  (or https://www.python.org/downloads/)" >&2
    echo "  Debian: sudo apt install python3 python3-venv" >&2
    exit 1
}

echo "==> Using $("$PY" --version)"
echo "==> Creating venv at $VENV"
"$PY" -m venv "$VENV"

echo "==> Installing kemi (with fast crypto) from $REPO@$BRANCH"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade "kemi[crypto] @ git+$REPO@$BRANCH" \
    || "$VENV/bin/pip" install --quiet --upgrade "kemi @ git+$REPO@$BRANCH"

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/kemi" "$BIN_DIR/kemi"

echo ""
echo "⚓ Kemi installed: $("$VENV/bin/kemi" --version)"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "   add to your shell profile:  export PATH=\"\$PATH:$BIN_DIR\"" ;;
esac
echo ""
echo "   Set sail:   kemi join"
echo "   Learn:      kemi learn"
