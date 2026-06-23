# Running Kemi everywhere — iOS, Android, macOS, Windows, Linux

Kemi has two roles, and which platforms can play which is shaped by what each
OS actually allows. The honest map:

| Platform | Full node (provider/peer) | Client (chat, submit jobs, watch) |
|---|---|---|
| **Linux**   | ✅ native (`pip` / Docker / installer) | ✅ browser / PWA |
| **macOS**   | ✅ native (`pip` / installer / `.app`) | ✅ browser / PWA |
| **Windows** | ✅ native (`pip` / PowerShell / `.exe`) | ✅ browser / PWA |
| **Android** | ⚠️ via Termux (advanced) | ✅ **PWA** (install from browser) |
| **iOS**     | ❌ not allowed (OS sandbox) | ✅ **PWA** (Add to Home Screen) |

**Why not a full node on phones?** A Kemi node is a long-running process that
listens on TCP+UDP sockets and runs a Kademlia DHT. iOS forbids background
daemons and arbitrary sockets for App Store apps; Android allows it only with
heavy battery/background caveats. Phones are also rarely good 24/7 providers.
So the right design is: **desktops/servers run nodes; phones and browsers are
thin clients** that use a fleet over its HTTP API.

## The client that runs on all five: the PWA

Every node serves a dashboard that is a **Progressive Web App**. On any
device, open the dashboard URL and install it:

- **iOS (Safari):** Share → *Add to Home Screen* → a "Kemi" app icon.
- **Android (Chrome):** menu → *Install app* / *Add to Home Screen*.
- **Desktop (Chrome/Edge):** the install icon in the address bar.

It's offline-tolerant (a service worker caches the shell), mobile-responsive,
and talks to the fleet through `/api/*`. From your phone you can chat with the
fleet's AI, submit jobs, watch providers and invite friends — the heavy
compute runs on the fleet's desktop/server ships.

Point a phone at a desktop ship on the same Wi-Fi:
`kemi node --ui 8080 --ui-host 0.0.0.0` then browse to
`http://<desktop-ip>:8080` from the phone and install it.

## Full node install per desktop OS

```bash
# Linux / macOS — one-line installer (isolated venv + `kemi` on PATH)
curl -fsSL https://raw.githubusercontent.com/emred530-blip/Kemi/main/scripts/install.sh | sh

# Windows — PowerShell
irm https://raw.githubusercontent.com/emred530-blip/Kemi/main/scripts/install.ps1 | iex

# Any OS with Python 3.10+
pip install kemi          # then: kemi app
```

### No Python at all? Standalone desktop apps

Build a self-contained app (bundles Python) with PyInstaller, per OS:

```bash
sh packaging/build.sh
# macOS → dist/Kemi.app   Windows → dist\Kemi.exe   Linux → dist/Kemi
```

Distribute that single file; end users double-click it — no Python, no
terminal. (Code-signing/notarisation is the maintainer's step and needs
platform developer certificates.)

### Servers

```bash
docker compose up        # a containerised fleet
kemi service             # systemd (Linux) / launchd (macOS) auto-start unit
```

## Android full node (advanced, optional)

Inside [Termux](https://termux.dev): `pkg install python`, then
`pip install kemi` and `kemi node --provide`. Works, but Android battery and
background limits make a phone a poor always-on provider — prefer the PWA
client for phones.
