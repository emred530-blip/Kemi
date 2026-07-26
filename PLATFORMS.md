# Running Kemi everywhere — iOS, Android, macOS, Windows, Linux

Kemi has two roles. Which platforms can fill which role is determined by what
each OS allows. The support matrix:

| Platform | Full node (provider/peer) | Client (chat, submit jobs, watch) |
|---|---|---|
| **Linux**   | Yes — native (`pip` / Docker / installer) | Yes — browser / PWA |
| **macOS**   | Yes — native (`pip` / installer / `.app`) | Yes — browser / PWA |
| **Windows** | Yes — native (`pip` / PowerShell / `.exe`) | Yes — browser / PWA |
| **Android** | Limited — via Termux (advanced) | Yes — **PWA** (install from browser) |
| **iOS**     | No — not allowed (OS sandbox) | Yes — **PWA** (Add to Home Screen) |

**Why not a full node on phones?** A Kemi node is a long-running process that
listens on TCP+UDP sockets and runs a Kademlia DHT. iOS forbids background
daemons and arbitrary sockets for App Store apps; Android allows it only with
heavy battery/background caveats. Phones are also rarely good 24/7 providers.
The design follows from this: **desktops and servers run nodes; phones and
browsers are thin clients** that reach the network over its HTTP API.

## The client that runs on all five: the PWA

Every node serves a dashboard that is a **Progressive Web App**. On any
device, open the dashboard URL and install it:

- **iOS (Safari):** Share → *Add to Home Screen* → a "Kemi" app icon.
- **Android (Chrome):** menu → *Install app* / *Add to Home Screen*.
- **Desktop (Chrome/Edge):** the install icon in the address bar.

It is offline-tolerant (a service worker caches the shell), mobile-responsive,
and talks to the network through `/api/*`. From a phone you can chat with the
network's AI, submit jobs, monitor providers and invite others. The heavy
compute runs on the network's desktop and server nodes.

Point a phone at a desktop node on the same Wi-Fi:
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

### Standalone desktop apps (no Python required)

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
docker compose up        # a containerised network
kemi service             # systemd (Linux) / launchd (macOS) auto-start unit
```

## Android full node (advanced, optional)

Inside [Termux](https://termux.dev): `pkg install python`, then
`pip install kemi` and `kemi node --provide`. This works, but Android battery
and background limits make a phone a poor always-on provider. Use the PWA
client on phones instead.
