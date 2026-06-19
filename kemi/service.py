"""``kemi service``: keep a provider running across reboots.

Generates and installs an OS service unit — systemd (Linux) or launchd
(macOS) — so a ship rejoins the fleet automatically after a restart. This is
what turns "I ran it once" into "my spare machine is always earning".
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SERVICE_NAME = "kemi"


def _kemi_command(extra_args: list[str]) -> str:
    exe = shutil.which("kemi")
    base = [exe] if exe else [sys.executable, "-m", "kemi"]
    return " ".join(base + ["node", "--provide", *extra_args])


def systemd_unit(extra_args: list[str]) -> str:
    return f"""[Unit]
Description=Kemi compute-sharing node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={_kemi_command(extra_args)}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def launchd_plist(extra_args: list[str]) -> str:
    exe = shutil.which("kemi")
    program = [exe] if exe else [sys.executable, "-m", "kemi"]
    args = program + ["node", "--provide", *extra_args]
    items = "\n".join(f"    <string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.kemi.node</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
"""


def install(extra_args: list[str], *, write: bool = True) -> tuple[Path, str, str]:
    """Return (path, content, activate_hint). With ``write=False`` only render
    (used by tests and ``--dry-run``)."""
    platform = sys.platform
    if platform == "darwin":
        path = Path("~/Library/LaunchAgents/com.kemi.node.plist").expanduser()
        content = launchd_plist(extra_args)
        hint = f"launchctl load {path}"
    elif platform.startswith("linux"):
        path = Path("~/.config/systemd/user/kemi.service").expanduser()
        content = systemd_unit(extra_args)
        hint = "systemctl --user daemon-reload && systemctl --user enable --now kemi"
    else:
        raise RuntimeError(f"unsupported platform for service install: {platform}")
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return path, content, hint
