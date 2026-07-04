# Changelog

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
