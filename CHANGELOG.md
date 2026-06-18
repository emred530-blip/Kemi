# Changelog

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
