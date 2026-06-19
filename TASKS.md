# Kemi — Task Backlog

**Status: 1.0.0 released.** 14 of 18 backlog items shipped (T1, T3–T7,
T11, T13–T18). The four still open need things this sandbox can't
genuinely test — real NATs (T2), `ffmpeg` (T8), `wasmtime` (T9) — or are
post-1.0 polish (T10 fleet map, T12 bilingual dashboard). They stay as the
post-1.0 roadmap rather than untested code.

Pick by ID. Status: ⬜ open · 🟦 in progress · ✅ done.

## A — Network & Protocol

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T1 | ✅ | **Stake-weighted witness committees** — higher-earned ships preferred as witnesses near a sender's key; attacker must out-earn real ships *(v0.15)* | High | Medium |
| T2 | ⬜ | **UDP hole-punching** — direct NAT-to-NAT task traffic coordinated via the DHT, relay stays as fallback; cuts relay load and latency | High | High |
| T3 | ✅ | **Protocol specification (SPEC.md)** — wire formats, DHT keys, tx/envelope schemas, witness flow; enables clients in other languages *(v0.14)* | Medium | Medium |
| T4 | ✅ | **Result cache** — content-addressed LRU of (task, items, params) results on providers; identical work served instantly *(v0.14)* | Medium | Medium |

## B — AI

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T5 | ✅ | **Real transformer layer sharding** — a real GPT forward pass split into per-ship layer groups via `ai.shard`; no machine holds the whole model, shards redundancy-verified and e2e-encrypted *(v0.12)* | High | High |
| T6 | ✅ | **Multi-model marketplace** — providers advertise model names (llama3.2, mistral…) in their records; consumers pick with `--model`; dashboard shows the model column *(v0.11)* | High | Medium |
| T7 | ✅ | **`ai.embed` task** — batch embeddings over the fleet (Ollama embed API + deterministic mock); unlocks RAG-style workloads *(v0.11)* | Medium | Low |

## C — Workloads

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T8 | ⬜ | **ffmpeg transcoding task** — capability-gated audio/video conversion with chunked base64 transport; the first heavyweight real-world workload | Medium | Medium |
| T9 | ⬜ | **WASM runner** — run untrusted custom code safely (wasmtime, capability-gated); ends the allowlist-only limitation | High | High |

## D — Dashboard & UX

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T10 | ⬜ | **Fleet map** — live network topology visualisation in the dashboard (ships, relay links, traffic) | Medium | Medium |
| T11 | ✅ | **Job templates** — one-click presets (hash/wordcount/AI/embed/aggregate) in the dashboard *(v0.15)* | Medium | Low |
| T12 | ⬜ | **Bilingual dashboard** — TR/EN language toggle in the panel | Low | Low |

## E — Operations

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T13 | ✅ | **Metrics & structured logs** — Prometheus `/metrics`, per-node counters in node.info *(v0.14)* | Medium | Low |
| T14 | ✅ | **Service install** — `kemi service` generates systemd/launchd units so providers survive reboots *(v0.14)* | Medium | Low |
| T15 | ✅ | **Docker image + compose fleet** — one-command containerised fleet *(v0.14)* | Medium | Low |

## F — Economy & Security

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T16 | ✅ | **Dynamic pricing** — `--dynamic-price` surges price with load; base stays the floor *(v0.14)* | Medium | Medium |
| T17 | ✅ | **Economy simulation** — `kemi economy`: conserved supply, PoW-gated faucet, credits flow to providers *(v0.15)* | Medium | Medium |
| T18 | ✅ | **Protocol fuzzing pass** — malformed-message matrix across every TCP/UDP handler; crash-free guarantee under garbage input *(v0.11)* | High | Medium |

## Blocked on the user (not code)

| ID | Status | Task |
|----|--------|------|
| U1 | ⬜ | Merge branch to `main`, make the repo public |
| U2 | ⬜ | Register PyPI trusted publishing (repo + `release.yml` + env `pypi`) |
| U3 | ⬜ | Push tag `v0.10.0` → CI tests, builds and publishes to PyPI automatically |
