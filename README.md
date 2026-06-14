# Kemi — The Decentralised P2P Compute Sharing Network

Kemi does for **computing power** what BitTorrent did for files. Anyone can
rent out their spare CPU/GPU for **credits**; anyone can spend those credits
to run AI inference or other heavy workloads on the **fleet** — the swarm of
peers. The goal: make centralised data centres matter less.

**Since v0.2 there is no central component anywhere** — no tracker, no ledger
server, no special roles; every participant runs the same `kemi` node.
**v0.3** added end-to-end encryption and pipeline parallelism. **v0.4** the
embedded live dashboard. **v0.5** real LLM inference via Ollama, live token
streaming, real workloads and 25-node scale tests. **v0.6** instant
double-spend *prevention* via witness committees and streaming through
relays. **v0.7** one-command onboarding, invite codes, LAN auto-discovery,
ship names and ranks. **v0.8 goes global**: English-first everywhere, with
Turkish command aliases kept on board. **v0.9 is the usability release**: a
three-line Python library API, `kemi chat` (a streaming AI REPL), `kemi
doctor` diagnostics and plain-text job input. **v0.10 is seaworthy**:
per-IP rate limits and connection caps, ledger checkpointing with fast
snapshot bootstrap, and chat built into the dashboard.

```
            ╭───────────────  DISCOVERY: Kademlia DHT (UDP)  ───────────────╮
            │  provider records live on the K nodes XOR-closest to each     │
            │  task key; signed and short-lived                             │
            ╰────────────────────────────────────────────────────────────────╯
   ┌──────────┐      ┌──────────┐      ┌──────────┐      ┌──────────┐
   │  SHIP A  │◄────►│  SHIP B  │◄────►│  SHIP C  │◄────►│  SHIP D  │
   │ provider │      │ consumer │      │ peer +   │◄═════│ (behind  │
   │ 0.5 cr/it│      │          │      │ relay    │ persistent NAT) │
   └────▲─────┘      └────┬─────┘      └──────────┘ conn └──────────┘
        │   chunks +      │
        ╰── signed pay ───╯
            ╭────────────────────────────────────────────────────────────────╮
            │  LEDGER: a gossip-replicated CRDT set of signed transfers —    │
            │  every peer holds a full replica                               │
            ╰────────────────────────────────────────────────────────────────╯
```

## Join in 60 seconds

No required dependencies (Python ≥ 3.10 standard library is enough;
`pynacl` recommended):

```bash
# one-line install (isolated venv + `kemi` on your PATH):
curl -fsSL https://raw.githubusercontent.com/emred530-blip/Kemi/main/scripts/install.sh | sh

# or from a clone:
pip install -e .            # for fast signatures: pip install -e ".[crypto]"

kemi join                   # that's it.
```

The wizard does the rest: names your ship (derived from your identity, like
"swift-gull-42"), **auto-discovers a fleet on your Wi-Fi** (LAN discovery),
makes you the founding ship if there is none, asks whether you want to share
compute, opens the live dashboard and prints the **invite code** you hand to
friends.

```bash
kemi join --invite kemi1-mfzgc…   # join with a friend's invite code
kemi invite --peer IP:7700        # mint an invite code for your fleet
kemi learn                        # 3-minute interactive tour on a live fleet
kemi chat --peer IP:7700          # talk to the fleet's AI, replies stream live
kemi doctor                       # ✓/✗ readiness report for this machine
kemi demo --ui 8080               # demo fleet + live dashboard
```

The CLI is bilingual — Turkish aliases ship with it: `katil/join`,
`filo/providers`, `bakiye/balance`, `calistir/run`, `durum/status`,
`kimlik/id`, `ogren/learn`, `davet/invite`.

### Ships and ranks

In Kemi (an old Turkish word for *ship*) every node is a **ship**, and humans
see ship names instead of hex digests. As your ship earns credits by sharing
compute, it climbs ranks — derived purely from the ledger, so ranks cannot
be faked:

| Credits earned | Rank |
|---|---|
| 0+ | · Cabin Boy |
| 25+ | ⚓ Deckhand |
| 100+ | ⚓⚓ Helmsman |
| 300+ | ⚓⚓⚓ First Mate |
| 1000+ | ★ Captain |
| 5000+ | ★★ Admiral |

Your ship name and rank appear on the dashboard, in `kemi id` and in fleet
listings.

## How decentralisation works

| Problem | Solution |
|---|---|
| **Peer discovery** | Kademlia DHT (the same approach as BitTorrent's trackerless mode, BEP 5). Providers publish signed records under per-task keys on the XOR-closest K nodes; records expire by TTL. Any running peer is a valid entry point. |
| **Identity** | Ed25519 keypairs. `node_id = sha256(pubkey ‖ nonce)` must start with N zero bits: minting an identity costs **proof-of-work**, which makes Sybil attacks and faucet farming expensive. libsodium via PyNaCl when present, pure-Python RFC 8032 otherwise. |
| **Payment** | No escrow, no tracker: a **per-chunk Ed25519-signed credit transfer** travels with each chunk. The ledger is a *grow-only set* of signed transactions (CRDT): it replicates by gossip and every replica converges regardless of arrival order. |
| **Double-spending** | Two layers. **Prevention:** before accepting a payment, the provider consults the sender's deterministic **witness committee** (the K nodes closest to `sha256("kemi:witness:"+sender)`); witnesses lock the first transfer seen per `(sender, seq)`, co-sign a receipt and **veto** conflicts with evidence — of two racing payments at most one wins. **Detection:** even if a veto is missed, conflicting signed transactions are mathematical proof in the gossip; one is counted deterministically and the account is flagged forever. The committee adapts: it shrinks to whatever is reachable in small fleets and falls back to optimistic mode when alone (liveness is never lost). |
| **Fabricated results** | `--redundancy 2+`: every chunk runs on distinct providers, result fingerprints are compared, **majority wins**. Losers take a local reputation hit. |
| **Reputation** | Each node keeps local (subjective) scores fed only by *first-hand* experience — shared reputation is trivially poisoned, first-hand experience is not. Double-spend evidence, by contrast, is objective and travels with the transactions themselves. |
| **NAT traversal** | Every response echoes the requester's *observed* external address (no STUN server needed). A NATed provider keeps a persistent connection to any reachable peer, which relays its task traffic (TURN-style). Streaming multiplexes through the same session. |
| **Isolation** | Tasks are allowlisted (arbitrary code from the network is never executed) and each chunk additionally runs in a separate process with hard `rlimit` caps (CPU seconds / memory / file descriptors). |
| **Privacy** | Chunk contents and results are **end-to-end encrypted** between consumer and provider (byte-compatible with NaCl `crypto_box`: X25519 + XSalsa20-Poly1305). Keys derive from the Ed25519 identities both sides already have — no handshake; relays only ever see ciphertext. Without PyNaCl a pure-Python implementation takes over; both produce identical bytes (tested against libsodium). |
| **Large models** | **Pipeline parallelism**: with `run_pipeline` each stage's output feeds the next stage, so a layer-sharded model can run across providers none of which could host the whole model. Every stage gets the full scheduler treatment (chunking, retries, majority voting, signed payment, encryption). |
| **GPU** | GPUs are discovered via `nvidia-smi` and advertised in resource records; the `transformers` backend can run on GPU. |
| **Abuse resistance** | Per-source-IP token buckets on TCP requests and DHT datagrams, per-IP and global connection caps, relay-session quotas and DHT storage limits — one hostile host cannot starve the fleet, and limits never throttle a healthy local swarm. |
| **Ledger growth** | Deterministic, content-hashed **checkpoints**: `prune()` folds history into a baseline (balances, spend sequences, lifetime earnings and double-spend verdicts survive; pre-checkpoint replays are rejected as stale). New ships **fast-bootstrap** by adopting a snapshot verified across multiple independent sources instead of replaying history. |

### Trust model (the honest summary)

Payments travel with chunk requests, so a malicious provider can keep at most
**one chunk's price** without delivering — the same way BitTorrent bounds risk
with small pieces. The ledger offers eventual consistency rather than instant
finality; since v0.6 the witness committee *prevents* double-spends in most
cases, with the evidence-and-flagging layer as the safety net wherever the
committee is unreachable. Reputation plus the proof-of-work identity cost
makes repeated attacks economically pointless.

## Running a real fleet

```bash
# 1. Start the first peer (it has no special role; it is merely first)
kemi node --port 7700

# 2. On every machine that shares compute (--lan: auto-discovery on the LAN)
kemi node --provide --peer FIRST_PEER_IP:7700 --price 0.5 --lan
#    Behind NAT? --force-relay  (auto-detection is attempted too)

# 3. View the fleet
kemi providers --peer FIRST_PEER_IP:7700

# 4. Submit a job: chunked, cross-checked on 2 distinct providers
echo '["a","b","c","d"]' | kemi run --peer FIRST_PEER_IP:7700 \
    --task hash.sha256 --input - --chunk-size 2 --redundancy 2

# 5. A multi-stage pipeline job (the basis for layer-sharded models)
echo '[[0.1,0.2,0.3]]' | kemi pipeline --peer FIRST_PEER_IP:7700 --input - \
    --stages '[{"task":"ai.layer","params":{"layer":0}},{"task":"ai.layer","params":{"layer":1}}]'

# 6. Balance, identity, peer health
kemi balance --peer FIRST_PEER_IP:7700
kemi id
kemi status --peer FIRST_PEER_IP:7700

# 7. Live dashboard: providers, ledger, reputation + submit jobs from the browser
kemi node --peer FIRST_PEER_IP:7700 --ui 8080   # http://127.0.0.1:8080/
```

All job traffic is **end-to-end encrypted by default** (provider records
advertise the `e2e` capability; opt out with `Job(encrypt=False)`).

### Use it as a Python library

Kemi is a library, not just a CLI - embedding the fleet in your own
application takes three lines:

```python
import asyncio, kemi

async def main():
    async with kemi.connect(peer="FIRST_PEER_IP:7700") as fleet:
        hashes = await fleet.run("hash.sha256", ["a", "b", "c"])
        answer = await fleet.generate("Why do P2P networks matter?")
        async for token in fleet.stream("Tell me a story"):   # live tokens
            print(token, end="", flush=True)
        report = await fleet.run("data.aggregate", [records],
                                 params={"group_by": "city", "op": "sum"},
                                 redundancy=2, full_report=True)
        print(fleet.ship, fleet.rank(), await fleet.balance())

asyncio.run(main())
```

`connect()` also accepts `invite="kemi1-…"`, `lan=True` (auto-discover on
the local network), `share=True` (offer this machine's compute while
connected) and `identity_path=`/`ledger_path=` for a persistent wallet.
`fleet.pipeline([...], items)` runs multi-stage pipeline jobs.

### Chat with the fleet

```bash
kemi chat --peer FIRST_PEER_IP:7700
```

A conversational REPL: replies stream in token by token from the cheapest
reputable provider, every reply shows its cost, and the transcript is kept
locally so context-capable models (Ollama) hold a real conversation - the
fleet itself stays stateless. Commands: `/balance`, `/clear`, `/quit`.

### Plain-text jobs

`kemi run --lines` treats input as plain text, one item per non-empty
line - process a whole file across the fleet without writing JSON:

```bash
kemi run --peer ... --task text.wordcount --input corpus.txt --lines
```

### When something feels off: `kemi doctor`

A ✓/✗ readiness report with actionable hints: Python version, crypto
backend, identity file, task execution, sandbox isolation, LAN multicast,
GPU presence, Ollama availability and (with `--peer`) reachability and
latency of a fleet peer.

### AI inference (with real models)

```bash
# Dependency-free deterministic mock backend (default):
kemi node --provide --peer ... --ai-backend mock

# A REAL local model — via Ollama (the recommended path):
#   1) install Ollama from https://ollama.com
#   2) ollama pull llama3.2
#   3) open your compute to the fleet:
kemi node --provide --peer ... --ai-backend ollama --ai-model llama3.2

# Alternative: Hugging Face pipeline in-process (GPU used when present):
pip install "kemi[ai]"
kemi node --provide --peer ... --ai-backend transformers
```

```bash
# Batch generation:
echo '["Why do P2P networks matter?"]' | \
    kemi run --peer ... --task ai.generate --input - --params '{"max_tokens": 64}'

# LIVE streaming: tokens land on your screen as the model produces them
# (end-to-end encrypted):
echo '["Why do P2P networks matter?"]' | \
    kemi run --peer ... --task ai.generate --input - --stream

# Pick a specific model from the marketplace:
kemi models --peer ...                       # what's on offer
kemi run --peer ... --task ai.generate --input - --model llama3.2 --stream

# Embeddings (the RAG building block):
echo '["doc one", "doc two"]' | \
    kemi run --peer ... --task ai.embed --input - --lines
```

Streaming is the one path where payment is taken *first* (otherwise the
consumer could vanish after the last token); exposure is still bounded by a
single chunk's price, and streaming-capable providers advertise the `stream`
badge. **NATed providers can stream too**: tokens multiplex through the relay
session, and the relay sees only ciphertext. The dashboard shows `ai.generate`
output growing in real time.

### The live dashboard

`--ui PORT` gives every node a dependency-free control panel:

- **Chat:** talk to the fleet's AI right in the panel — replies stream in
  live, each tagged with its cost and the ship that produced it.
- **Providers (live):** price, reputation score, CPU/GPU, direct/relay path,
  e2e and stream badges — banned/flagged nodes filtered out automatically.
- **Submit jobs:** pick a task, paste JSON items, set chunk size/redundancy;
  follow status and cost live; download full results as JSON.
- **Ledger:** balance with a time-series sparkline, latest transfers, and any
  double-spend evidence (⚑).
- **Reputation:** peer scores as this node sees them.

The dashboard binds to `127.0.0.1` only by default (it has no auth; put it
behind a reverse proxy you trust if you must expose it). It refreshes every
2 seconds.

## Built-in task types

| Task | Description | Sandboxed |
|---|---|---|
| `ai.generate` | Text generation (mock/Ollama/transformers; live streaming; per-model via `--model`) | in-process (model memory) |
| `ai.embed` | Batch text embeddings — the RAG building block | in-process (model memory) |
| `ai.layer` | Layer-sharded model strip (pipeline parallelism) | ✓ |
| `data.aggregate` | Map-reduce: group JSON records + sum/avg/min/max/count | ✓ |
| `crypto.pbkdf2` | PBKDF2-HMAC-SHA256 key hardening (real CPU work) | ✓ |
| `compress.gzip` | Batch compression (text or base64 binary) | ✓ |
| `sci.matmul` | Real BLAS matrix multiplication — *auto-advertised when numpy is installed* | ✓ |
| `hash.sha256` | Multi-round SHA-256 | ✓ |
| `math.matmul` | Pure-Python matrix multiply (benchmark) | ✓ |
| `text.wordcount` | Word/char/line counts | ✓ |

`sci.matmul` demonstrates the **capability-gated task** pattern: a task
registers only on providers that have its dependency and is advertised only
in their DHT records — heavy workloads like ffmpeg/video transcoding plug in
the same way. New capabilities are added by registering tasks in
`kemi/tasks.py`; the security boundary is always the name-based allowlist.

## Architecture

| Module | Responsibility |
|---|---|
| `kemi/crypto.py` | Ed25519 (PyNaCl → pure-Python fallback), canonical JSON, signed envelopes |
| `kemi/identity.py` | Proof-of-work keypair identities |
| `kemi/dht.py` | Kademlia DHT: k-buckets, iterative lookup, signed+TTL records, observed-address NAT detection |
| `kemi/discovery.py` | Provider announce/lookup on top of the DHT |
| `kemi/e2e.py` | End-to-end encryption: NaCl-box-compatible X25519 + XSalsa20-Poly1305 (pure-Python fallback) |
| `kemi/gossip_ledger.py` | Signed-transaction CRDT: gossip replication, double-spend evidence, seq reservation |
| `kemi/reputation.py` | Local reputation scores (Beta estimate) and bans |
| `kemi/sandbox.py` | rlimit-capped subprocess isolation |
| `kemi/node.py` | The unified peer: TCP services, gossip loops, provider service, witness committee, relay (both sides), GPU discovery |
| `kemi/consumer.py` | Chunking, scheduling, fault tolerance, majority verification, per-chunk signed payment, streaming |
| `kemi/protocol.py` | Wire protocol: length-prefixed JSON over TCP |
| `kemi/tasks.py`, `kemi/ai_backends.py` | Allowlisted tasks; pluggable AI backends (mock/Ollama/transformers) |
| `kemi/webui.py` | Embedded live dashboard (stdlib HTTP) |
| `kemi/names.py` | Character layer: ship names and ranks |
| `kemi/invite.py` | Invite codes (`kemi1-…`, no secrets) |
| `kemi/lan.py` | Zero-config LAN discovery (multicast beacon) |
| `kemi/tutorial.py` | `kemi learn`: interactive tour on a live fleet |
| `kemi/api.py` | High-level library API (`kemi.connect()` / `Fleet`) |
| `kemi/chat.py` | `kemi chat`: streaming conversational REPL |
| `kemi/doctor.py` | `kemi doctor`: machine readiness diagnostics |
| `kemi/cli.py`, `kemi/demo.py` | Bilingual CLI and the end-to-end demo |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

128 tests, including: rate-limit and connection-cap enforcement, checkpoint determinism/prune/stale-replay, fast bootstrap from a pruned quorum, dashboard chat, the three-line library API, chat turns with cost and history, doctor diagnostics, cross-backend crypto interoperability (the pure-Python
NaCl implementation is verified byte-for-byte against libsodium, plus RFC
7748/8439 vectors), PoW identities, DHT storage/lookup, ledger convergence
and double-spend proofs, the witness committee blocking a concurrent
double-spend race, live token streaming (with proof that no plaintext leaks
on the wire), streaming through a relay, pipeline composition, a 25-node
fleet, half the providers dying mid-job, restart-from-disk seq safety,
deterministic ship names, rank thresholds, invite-code round-trips, LAN
beacon discovery, and the tutorial running end to end.

## Roadmap

- **Stake-weighted witnesses:** weighting committee membership by earned
  credits on top of PoW (stronger Sybil resistance).
- **Full hole-punching:** direct NAT-to-NAT task traffic via UDP
  hole-punching alongside the relay.
- **Harder isolation:** container/WASM runner, filesystem and network
  namespaces, real GPU quotas.
- **Real model strips:** a backend that replaces `ai.layer`'s reference
  implementation with actual transformer layer groups (the pipeline
  infrastructure is ready).
