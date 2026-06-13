# Kemi — Task Backlog

Pick by ID. Status: ⬜ open · 🟦 in progress · ✅ done.

## A — Network & Protocol

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T1 | ⬜ | **Stake-weighted witness committees** — weight committee membership by earned credits on top of PoW; raises the economic cost of Sybil attacks on payment finality | High | Medium |
| T2 | ⬜ | **UDP hole-punching** — direct NAT-to-NAT task traffic coordinated via the DHT, relay stays as fallback; cuts relay load and latency | High | High |
| T3 | ⬜ | **Protocol specification (SPEC.md)** — wire formats, DHT keys, tx/envelope schemas, witness flow; enables clients in other languages | Medium | Medium |
| T4 | ⬜ | **Result cache** — content-addressed cache of (task, items, params) results on providers; identical work is answered instantly and cheaper | Medium | Medium |

## B — AI

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T5 | ⬜ | **Real transformer layer sharding** — replace `ai.layer`'s reference math with actual model layer groups so models too big for one ship run across the fleet (pipeline infra is ready) | High | High |
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
| T11 | ⬜ | **Job templates & result views** — one-click presets for common jobs, table/chart rendering for aggregate results | Medium | Low |
| T12 | ⬜ | **Bilingual dashboard** — TR/EN language toggle in the panel | Low | Low |

## E — Operations

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T13 | ⬜ | **Metrics & structured logs** — Prometheus-format `/api/metrics`, JSON log option; what fleet operators need | Medium | Low |
| T14 | ⬜ | **Service install** — `kemi service install` generating systemd/launchd units so providers survive reboots | Medium | Low |
| T15 | ⬜ | **Docker image + compose fleet** — one-command containerised fleet for server operators | Medium | Low |

## F — Economy & Security

| ID | Status | Task | Impact | Effort |
|----|--------|------|--------|--------|
| T16 | ⬜ | **Dynamic pricing** — providers auto-adjust price from their load/queue depth; market finds equilibrium | Medium | Medium |
| T17 | ⬜ | **Economy simulation report** — model the genesis faucet, credit sinks and long-run inflation; recommend parameters | Medium | Medium |
| T18 | ✅ | **Protocol fuzzing pass** — malformed-message matrix across every TCP/UDP handler; crash-free guarantee under garbage input *(v0.11)* | High | Medium |

## Blocked on the user (not code)

| ID | Status | Task |
|----|--------|------|
| U1 | ⬜ | Merge branch to `main`, make the repo public |
| U2 | ⬜ | Register PyPI trusted publishing (repo + `release.yml` + env `pypi`) |
| U3 | ⬜ | Push tag `v0.10.0` → CI tests, builds and publishes to PyPI automatically |
