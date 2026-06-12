# Changelog

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
