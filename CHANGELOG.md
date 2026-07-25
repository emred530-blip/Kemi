# Changelog

## 1.10.0 — The keeper's panel (harbor admin)
- `/admin` on every harbor: a key-gated keeper's panel (key printed at
  `kemi web` start, or set with `--admin-key`), designed in scan order —
  status alerts first, then headline tiles (balance, 24h fleet spend,
  questions, active guests, estimated questions remaining, ships online),
  then the hourly-spend economy chart, the guest ledger and fleet quality.
- Guest operations that act immediately: gift credits, ban/unban (a banned
  guest is refused mid-session with an honest message), and a live faucet
  editor — all persisted to `harbor.json` along with the ask records the
  economy chart is built from.
- Alert engine encodes state, not decoration: no providers → critical,
  balance below ~25 questions → warning, flagged double-spenders → serious,
  >20% unanswered in 24h → warning; every alert ships icon + label, never
  color alone (the palette validator caught the classic red/green CVD trap
  and the status colors were fixed accordingly).
- Fleet quality table joins live provider records with the reputation
  book: model (real vs mock pill), price, score, good/bad counts,
  streaming capability. Chart follows the dataviz rules: single hue,
  single axis, hover tooltips, recessive grid, tabular numerals.
- Deliberately NOT in the admin panel: brain/voice/node-health segments —
  those are ship captaincy and live in the `kemi app` dashboard; the
  keeper's panel runs the harbor. Admin auth uses constant-time key
  comparison; admin URL and key are printed at startup.

## 1.9.1 — The night-sea identity (UI/UX facelift)
- The harbor got a designed visual identity instead of a generic dark
  theme: a "night sea" world — deep navy grounds, brass (credits/ledger)
  and phosphor teal (live fleet) accents, a serif captain's-log display
  face for the brand and hero, mono tabular numerals for everything
  ledger-like.
- New harbor landing: a hero thesis ("Merkezi olmayan yapay zekâ"), a live
  harbor board (ships online · models · credits per ask) and one-tap
  starter questions; the hero folds away once the conversation starts.
  Refined chat bubbles with fleet avatars, dashed-ledger provenance stamps,
  a phosphor streaming cursor and buoy-light thinking dots. Floating
  glass composer with a brass send button.
- Micro-interactions throughout (message rise-in, beacon pulse, hover
  lifts) — all disabled under prefers-reduced-motion. Accessibility pass:
  aria-live chat log, labeled controls, visible focus rings, contrast
  checked. Phone header no longer overflows.
- The captain's dashboard was restyled to the same token family so the
  product reads as one system (brass primaries, phosphor status, elevated
  cards, sticky glass header) — markup and behavior untouched.

## 1.9.0 — The harbor: Kemi as a web app
- `kemi web` (alias `liman`) opens a public harbor: a mobile-first web app
  where anyone chats with the fleet from a browser — no install, no
  account. Visitors get a welcome-credit guest wallet (host-configurable
  `--faucet`), their questions are answered by ships in the fleet, and
  every reply is stamped `⚓ credits · ship · model` as visible proof the
  answer came from another user's machine, not a central server.
- The harbor host's node pays providers on the real ledger and meters each
  guest wallet by exactly what their questions cost; broke guests are
  pointed at `pip install kemi && kemi app` to join the fleet with their
  own ship. Anyone can open their own harbor — decentralisation lives one
  level up.
- Guest wallets and chat history persist in `$KEMI_HOME/harbor.json`
  across harbor restarts. Per-guest cooldowns, message caps, idle-guest
  eviction and a harbor-wide concurrency cap keep one visitor from
  draining the host. Turkish/English UI.
- LAUNCH.md gained a "web uygulaması" go-live section.

## 1.8.1 — Streaming payments made airtight
Found by an adversarial multi-agent review of 1.8.0; all three confirmed
findings fixed:
- Payment receipts: a provider now sends an `accepted` frame the moment it
  commits the consumer's transfer. A consumer that abandons a stalled
  stream after that receipt mirrors the payment into its own ledger replica
  before hiring the next ship — previously the forgotten transfer could
  overdraw the account on failover and get it permanently flagged as a
  double-spender.
- Warming liveness: until the first token, providers emit a `warming` frame
  every 5 s (e.g. while a model loads into memory), so the first-token
  timeout only abandons dead-silent ships — a healthy Ollama ship cold-
  starting a 7B model is no longer dumped for a mock answer. First-token
  knobs are plumbed through `Fleet.stream`.
- Real-vs-mock tier preference now applies only to tasks the chat backend
  actually serves (`ai.generate`, `ai.embed`); sharded-layer and other
  `ai.*` tasks keep pure reputation/price ranking.
- SPEC: documented the receipt/warming frames and the trust limitation of
  self-advertised model names.

## 1.8.0 — The fleet answers people
- When someone asks the fleet a question, a ship serving a *real* model
  (Ollama, transformers) now always outranks a mock ship, however cheap the
  mock is — people get real answers whenever one is on offer. Non-AI tasks
  keep the reputation/price order.
- No more hanging chats: a provider that accepts a stream but produces no
  output is abandoned after a short first-token timeout and the next ship is
  tried automatically; the stall is fed to reputation and the synapse brain.
- The OpenAI gateway got an overall per-request deadline (504 Gateway
  Timeout instead of a stuck client) and speaks errors in-band on SSE
  streams instead of silently dropping the connection.
- Every dashboard chat reply is now stamped with its provenance —
  `[credits · ship · model]` — visible proof the answer came from another
  user's machine, not a central server. Chat placeholder localised to
  Turkish; `chat_once` returns the serving model.
- `kemi doctor` now lists the models your local Ollama has pulled and prints
  the exact command that turns your machine into an answering ship earning
  credits: `kemi node --provide --ai-backend ollama --ai-model <model>`.

## 1.7.0 — The captain's voice bridge
- Speak to your ship: the dashboard's new "Captain's bridge" card takes
  spoken orders (Web Speech API, Turkish) or typed commands and answers out
  loud — status reports, provider listings, submitting jobs, asking the
  fleet's AI, language toggle, invites, brain reports.
- Understanding is a synapse net, not keyword rules (`kemi/voice.py`): 
  utterances are stem-hashed into features and classified by the same
  online-learning network that powers provider selection, pre-seeded with
  Turkish and English phrasings. Unknown orders are refused honestly, and
  one click teaches the bridge what you meant — it adapts to how *you*
  speak, persisting to `$KEMI_HOME/voice.json`.
- Cross-entropy output gradients in `SynapseNet`: confidently-wrong
  predictions now get strong corrections instead of stalling at saturated
  sigmoids (benefits the fleet brain too).

## 1.6.0 — The synapse brain (self-training)
- `kemi/synapse.py`: a pure-stdlib online neural network that trains itself
  from the ship's own experience — every chunk a provider serves (or botches)
  becomes a training example the moment it happens. No dataset, no labels,
  no human in the loop.
- The brain seasons provider ranking (70% reputation + 30% prediction) once
  it has seen enough real outcomes; before that it learns silently in the
  background. Persists to `$KEMI_HOME/brain.json` and survives restarts.
- Dashboard "Synapse brain" card: live synapse visualisation (green
  excitatory / red inhibitory, thickness = |weight|), loss curve, step and
  accuracy counters — watch your ship learn in real time.

## 1.5.1 — Embeddings on sandboxed providers
- Fix: `ai.embed` (and therefore `/v1/embeddings`, `fleet.embed()`,
  `kemi search`) failed on default providers because the sandbox subprocess
  has no AI backend — embed now runs in-process like `ai.generate`.
  Found by end-to-end verification against a real node; regression-tested
  with a sandboxed provider.
- LAUNCH.md quickstart used the wrong `kemi run` syntax; corrected.

## 1.5.0 — Wallet and fairer reputation
- `kemi wallet` / `cuzdan`: balance, rank and a recent transaction history
  (direction, counterparty ship, signed amount) for your ship.
- Reputation decay: transient evidence (failed/mismatched chunks) fades over
  time so a node recovers from an old blip, while double-spend condemnation
  stays permanent. Decays hourly on running nodes; legacy reputation DBs
  migrate automatically.

## 1.4.0 — OpenAI-compatible gateway
- `kemi serve`: a localhost endpoint speaking the OpenAI REST API
  (`/v1/chat/completions` incl. streaming SSE, `/v1/embeddings`, `/v1/models`)
  backed by the fleet. Point Cursor, Continue, LangChain, the `openai` SDK or
  any OpenAI-compatible client at it — no API key, no account, no rewrite; the
  whole existing AI-tooling ecosystem runs on Kemi.

## 1.3.0 — Fleet map, media transcoding, RAG CLI
- Fleet map (T10): a live network-topology view in the dashboard — this ship
  at the centre, providers on a ring, relay links dashed, coloured by
  reputation, with per-ship tooltips.
- Media transcoding (T8): a capability-gated `media.transcode` task (audio/
  video via ffmpeg, base64 in/out, no shell, format allowlist) that registers
  only on providers that have ffmpeg — the first heavyweight real workload.
- `kemi search` / `ara`: RAG retrieval from the terminal (rank a file of
  documents against a query over the fleet).

## 1.2.0 — RAG, bilingual dashboard, downloadable apps
- RAG on the fleet: a `vector.search` task (cosine top-k retrieval) plus
  `Fleet.rag_search()` and `kemi search` — embed query + documents and rank
  them, all on the swarm. Pairs with `ai.embed` to make Kemi a real
  retrieval-augmented-generation backend.
- Bilingual dashboard (T12): a TR/EN toggle translating the panel's headings,
  welcome, buttons and labels, remembered in localStorage.
- Downloadable desktop apps: a GitHub Actions workflow builds a standalone
  Kemi for Linux/macOS/Windows with PyInstaller on each tagged release and
  attaches them to the GitHub Release — users download and run, no Python.

## 1.1.0 — Runs everywhere (iOS, Android, macOS, Windows, Linux)
- Progressive Web App: the dashboard is now installable on iOS (Add to Home
  Screen), Android (Install app) and every desktop browser — manifest,
  offline-tolerant service worker, app icons (pure-Python PNG, no Pillow),
  mobile-responsive layout, and iOS no-zoom inputs.
- `kemi app --phone` exposes the dashboard on the LAN so a phone on the same
  Wi-Fi can open and install it; localhost-only by default otherwise.
- Windows: `scripts/install.ps1` PowerShell one-line installer.
- Standalone desktop apps (no Python needed): PyInstaller recipe in
  `packaging/` (`.app` on macOS, `.exe` on Windows, binary on Linux) via
  `sh packaging/build.sh`.
- PLATFORMS.md: the honest map of full-node vs client support per OS.


## 1.0.0 — Release
First stable release. A fully decentralised peer-to-peer compute-sharing
network: Kademlia DHT discovery, Ed25519 proof-of-work identities, a
gossip-replicated CRDT credit ledger with witness-committee double-spend
prevention and checkpointing, redundancy-verified + end-to-end-encrypted
execution, NAT relay, sandboxing, DoS resistance, and sharded big-model
inference (run a model no single machine can hold). Batteries included:
`kemi app` one-command launch with an auto-opening dashboard, `kemi join`
onboarding, `kemi chat`/`shard`/`economy`/`service`, a three-line Python
API, Ollama/transformers backends, a multi-model marketplace, Prometheus
metrics, Docker, a one-line installer, and a full protocol spec (SPEC.md).
169 tests, green across repeated full runs. See RELEASE.md to publish.


## 0.15.0 — Trust, economy, and dashboard polish
- Stake-weighted witness committees (T1): among the candidates near a
  sender's witness key, established (higher-earned) ships are preferred as
  witnesses — an attacker must out-earn real ships, not just mint identities.
- Economy model + `kemi economy` (T17): a deterministic credit-flow
  simulation showing supply is conserved (no per-tx inflation), the faucet
  is PoW-gated, and credits concentrate toward providers (tit-for-tat).
- Dashboard job templates (T11): one-click presets (hash, word count, ask
  AI, embed, aggregate) fill the job form.

## 0.14.0 — Operational maturity
- Result cache (T4): providers serve identical deterministic work from a
  content-addressed LRU cache — the consumer still pays, the provider saves
  CPU. `ai.generate` is never cached.
- Metrics (T13): per-node counters (chunks served/failed, credits earned,
  cache hits, payments rejected) in `node.info` and a Prometheus `/metrics`
  endpoint on the dashboard.
- Dynamic pricing (T16): `--dynamic-price` surges the advertised price with
  load (up to 2x at full occupancy); the base price stays the floor a
  provider will always accept, so stale-record consumers are never rejected.
- `kemi service` (T14): generate and install a systemd (Linux) or launchd
  (macOS) unit so a provider rejoins the fleet after reboot.
- Docker (T15): `Dockerfile` and `docker-compose.yml` for a one-command
  containerised fleet (`docker compose up`).
- SPEC.md (T3): a complete protocol specification for writing clients in
  other languages.

## 0.13.0 — No coding required
- `kemi app` (alias `uygulama`): one command starts a sharing node and
  opens the dashboard in the browser — the no-terminal-after-this mode.
- `kemi node --open` auto-opens the dashboard too.
- Friendlier dashboard: a welcome banner and a one-click "Invite a friend"
  button that copies a ready-to-paste join command to the clipboard.
- Double-click launchers: `launchers/Kemi-Start.command` (macOS/Linux) and
  `Kemi-Start.bat` (Windows). README now opens with a "Not a coder?" path.

## 0.12.0 — Run a model no single machine can hold (T5)
- Sharded big-model inference: a real GPT-style transformer
  (`kemi/model.py`) whose layers split into contiguous groups, each group
  assigned to a *different* ship via the new `ai.shard` task. No participant
  ever holds — or even loads — the whole model; give it enough layers and it
  cannot fit on any one machine, yet the fleet runs it end to end.
- Verifiable: `ai.shard` is deterministic, so `redundancy=2` cross-checks
  every layer shard on distinct ships — a corrupt shard is outvoted (tested).
- Private: hidden states travel end-to-end encrypted; a provider sees only
  its slice's activations, never the prompt or output.
- `ShardedLLM` driver, `Fleet.shard_generate()`, and `kemi shard` /
  `kemi parcala`. Pure-Python reference weights keep it dependency-free and
  testable; point a provider at real trained weights and the same machinery
  serves an actual model.

## 0.11.0 — AI marketplace & fuzz-hardened
- Multi-model marketplace (T6): providers advertise their AI model name in
  signed records; consumers pin a model with `--model` / `model=`, list
  what's on offer with `kemi models` and `fleet.models()`. Dashboard and
  `kemi providers` show the model column.
- `ai.embed` task (T7): batch embeddings over the fleet (Ollama `/api/embed`
  + deterministic unit-norm mock); `fleet.embed()` and the RAG building
  block it unlocks.
- Protocol fuzzing pass (T18): a malformed/hostile-input matrix against
  every TCP handler and the DHT datagram path proves the node never
  crashes (garbage in → error or closed connection, never a dead peer);
  hardened `ledger.pull` cursor parsing found by the fuzzer.

## 0.10.0 — Seaworthy
- DoS protection: per-IP token-bucket rate limits and connection caps on
  TCP and the DHT (UDP), global connection ceiling, relay-session caps,
  DHT storage quotas. Limits are per-source: one abuser cannot starve
  the fleet. Configurable via `Limits`.
- Ledger checkpointing: deterministic content-hashed snapshots,
  `prune()` folds history into a baseline (balances, seqs, earnings and
  double-spend verdicts survive; replayed pre-checkpoint transfers are
  rejected as stale), and fast bootstrap — new ships adopt a snapshot
  verified across multiple sources instead of replaying history
  (`prune_above=` enables automatic pruning).
- Dashboard chat: talk to the fleet's AI from the browser; replies
  stream in live with per-reply cost and provider ship.
- Test suite de-flaked (3 consecutive full green runs) and extended to
  128 tests.

## 0.9.0 — Usability
- High-level Python API: `kemi.connect()` / `Fleet` — run jobs, generate,
  stream tokens, pipelines, balance/rank from three lines of code.
- `kemi chat`: streaming AI REPL with per-reply cost and local transcript.
- `kemi doctor`: ✓/✗ machine readiness diagnostics with actionable hints.
- `kemi run --lines`: plain-text input, one item per non-empty line.

## 0.8.0 — Going Global
- English-first across the CLI, wizard, tutorial, demo, dashboard and README;
  Turkish command/flag aliases preserved (`katil`, `filo`, `bakiye`, …).
- Ship names from English word lists; ranks Cabin Boy → Admiral.
- Release readiness: LICENSE (MIT), changelog, CI and PyPI publishing
  workflows, one-line installer script.

## 0.7.0 — Onboarding
- `kemi join`: 60-second wizard (ship identity, LAN auto-discovery,
  share/watch choice, dashboard, invite code).
- Invite codes (`kemi1-…`): connectivity-only, safe to share anywhere.
- Zero-config LAN discovery via multicast beacons.
- Character layer: deterministic ship names and ledger-derived ranks.
- `kemi learn`: 3-minute interactive tour on a live local fleet.
- Bilingual CLI aliases.

## 0.6.0 — Finality and reach
- Witness committees: instant double-spend *prevention* — racing payments
  hit the same deterministic committee and at most one wins; vetoes carry
  objective evidence.
- Live token streaming through relay sessions (NATed providers can stream).

## 0.5.0 — Real AI and real scale
- Ollama backend: genuine local LLM inference on the fleet (stdlib HTTP).
- Live token streaming (`kemi run --stream`), payment-first, e2e encrypted.
- Real workloads: `data.aggregate`, `crypto.pbkdf2`, `compress.gzip`,
  capability-gated `sci.matmul` (numpy).
- Dashboard: live streaming output, balance sparkline, result downloads.
- Scale tests: 25-node fleet, mid-job churn, restart-from-disk seq safety.

## 0.4.0 — The dashboard
- Embedded dependency-free live web dashboard (`--ui`): providers, jobs,
  ledger, reputation; submit jobs from the browser.

## 0.3.0 — Privacy and pipelines
- End-to-end encryption byte-compatible with NaCl `crypto_box`
  (X25519 + XSalsa20-Poly1305, pure-Python fallback verified against
  libsodium); relays see only ciphertext.
- Pipeline parallelism (`run_pipeline`) and the `ai.layer` reference task.
- `kemi status`.

## 0.2.0 — Decentralisation
- Tracker removed entirely. Kademlia DHT discovery, Ed25519 proof-of-work
  identities, gossip-replicated CRDT credit ledger with double-spend
  evidence, local reputation, TURN-style relay for NATed providers,
  rlimit sandbox, GPU discovery.

## 0.1.0 — MVP
- Tracker-based prototype: chunked jobs, escrowed credits, redundancy
  voting, pluggable AI backends.
