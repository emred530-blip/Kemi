"""PyInstaller entry point for the standalone desktop app.

Double-clicking the built app runs ``kemi app``: it joins/forms a fleet,
shares this machine's compute, and opens the dashboard in the browser — no
terminal, no Python install required by the end user.
"""

import sys

from kemi.cli import main

if __name__ == "__main__":
    # default to the friendly app mode when launched with no arguments
    sys.exit(main(sys.argv[1:] or ["app"]))
