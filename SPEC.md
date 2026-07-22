# Kemi Protocol Specification (v1)

This document defines the Kemi wire protocol precisely enough to write an
interoperable client or node in any language. The reference implementation is
the Python package in this repository; where this document and the code
disagree, the code is authoritative until this spec is corrected.

All multi-byte integers are big-endian. All hashes are SHA-256. All
signatures are Ed25519 (RFC 8032). JSON is UTF-8 with no insignificant
whitespace for anything that is hashed or signed (see *Canonical JSON*).

## 1. Identity

A node is an Ed25519 keypair plus a proof-of-work nonce.

```
node_id = sha256(pubkey_bytes || nonce_be64)   # 32 bytes, hex-encoded
```

The node id is valid only if the leading `POW_DIFFICULTY_BITS` (default 12)
bits of `node_id` are zero. Every peer verifies this before trusting a node
id. The `nonce` is found by brute force at identity-creation time.

## 2. Canonical JSON

Anything signed or hashed is first encoded as **canonical JSON**: UTF-8,
object keys sorted lexicographically, separators `","` and `":"` (no spaces),
non-ASCII preserved (not `\u`-escaped). `digest(x) = sha256(canonical(x))`
(hex).

## 3. Signed envelopes

A signed object travels as:

```json
{"payload": <object>, "pubkey": "<hex32>", "sig": "<hex64>"}
```

`sig = Ed25519_sign(seed, canonical(payload))`. An envelope *opens* iff the
signature verifies under `pubkey`. Consumers additionally check that the
payload's `node_id`/`from` field is bound to `pubkey` via §1.

## 4. TCP wire framing

Services use length-prefixed JSON over TCP:

```
[uint32 length][length bytes of UTF-8 JSON object]
```

Maximum message size is 32 MiB. A request is one framed message; the response
is one framed message on the same connection, after which it closes — except
streaming (§8) and relay sessions (§9), which exchange multiple frames.

Every response is a JSON object with `ok: true|false`; errors are
`{"ok": false, "error": "<code>", "detail": "<text>"}`.

## 5. Discovery — Kademlia DHT (UDP)

Same datagram port as the TCP service. Standard Kademlia (BEP-5 style) with
`K=8`, `ALPHA=3`. RPCs (JSON datagrams): `ping`, `find_node`, `find_value`,
`store`. Each datagram carries a `from` contact header
`{id, pubkey, pow_nonce, port}`; receivers verify the PoW (§1) before adding
the contact, and trust the *observed* source IP (this is also how a node
learns its own public address for NAT traversal).

Stored values must be signed envelopes (§3) with a fresh `ts` and are dropped
after their TTL or once `MAX_VALUES_PER_KEY` (64) is exceeded.

**Provider records** are published under `key = sha256("kemi:task:<task>")`
for each offered task, payload:

```json
{"kind":"provider","node_id","pow_nonce","host","port","price",
 "tasks":[...],"resources":{...},"relay":null|{host,port},
 "e2e":true,"stream":bool,"model":null|"<name>","embed":bool,"ts":<float>}
```

## 6. Ledger — signed-transfer CRDT

The credit ledger is a grow-only set of signed transactions, gossiped between
peers; replicas converge regardless of order. A transaction payload:

```json
{"kind":"tx","from":"<node_id>","pow_nonce":<int>,"to":"<node_id>",
 "amount":<float>,"seq":<int>,"ts":<float>}
```

Rules every replica applies identically:

* Reject unless the envelope opens, the `from` PoW is valid, `amount > 0`,
  `seq >= 1`, `to != from`, and `ts` is within `MAX_CLOCK_SKEW` (300 s) of now.
* `seq` is strictly increasing per sender. Two distinct transactions sharing
  `(from, seq)` are a **double-spend**: both are stored as evidence, the one
  with the smallest `tx_id` counts toward balances (deterministic), and the
  account is permanently flagged.
* `balance = GENESIS_CREDITS(100) + counted_in − counted_out`. Negative ⇒
  overdrawn ⇒ flagged.

Replication RPCs (TCP): `ledger.pull{cursor}` → `{txs,cursor}`,
`ledger.push{txs}`, and `ledger.snapshot` → `{snapshot,hash}` for fast
bootstrap (a content-hashed checkpoint; adopt only when independently fetched
copies agree on `hash`).

## 7. Compute — task execution

`task.execute` request:

```json
{"type":"task.execute","task":"<name>","payment":<signed tx envelope>,
 "items":[...],"params":{...}}
```

or, end-to-end encrypted, replace `items`/`params` with
`"enc": {"n":"<hex24>","c":"<hex>"}` (§10). The provider:

1. checks `task` is offered and `items` is a non-empty list;
2. validates `payment`: opens, PoW valid, `to == provider`, `amount >=
   price * len(items)`, sender not flagged/banned, sender balance sufficient;
3. consults the witness committee (§6.1) to block double-spends;
4. runs the task (tasks are an allowlist — arbitrary code is never executed);
5. applies + gossips the payment, then returns `{ok,results,charged}` (or
   `enc`-wrapped results).

Exposure is bounded to one chunk's price: payment travels with the request.

### 6.1 Witness committee

Before accepting payment, a provider asks the `K` DHT nodes closest to
`sha256("kemi:witness:<sender>")` via `tx.witness{tx}`. A witness stores the
tx; if it conflicts with a stored `(from, seq)` it returns
`{ok:false,error:"conflict",evidence:[...]}`, else a signed receipt. Racing
payments hit the same committee, so at most one is funded.

## 8. Streaming

`task.stream` (ai.generate only) returns multiple frames. Immediately after
committing the consumer's payment the provider sends an `{evt:"accepted"}`
receipt; until its first token it sends `{evt:"warming"}` liveness frames
every `STREAM_WARMING_INTERVAL` (5 s, e.g. while a model loads). Then
`{evt:"token",item,t}` … and a terminal
`{evt:"end",ok,results,charged,end:true}`. Payment is applied *before*
tokens flow; a consumer that received the receipt but abandons the stream
(silence past its first-token timeout) must mirror the payment into its own
ledger replica before hiring the next ship, or its balance check drifts
optimistic and a failover can overdraw the account. Frames may be
`enc`-wrapped.

Trust note: the `model` name in a provider record is *self-advertised* and
unverifiable — a ship can claim `llama3` while serving anything. Consumers
therefore only use it for tier preference (real-vs-mock) and pinning; quality
enforcement stays with reputation, redundancy voting and the consumer-side
brain, all of which act on observed outcomes, not claims.

## 9. Relay (NAT traversal)

A NATed provider opens a persistent connection with `relay.register{envelope}`
(`kind:"relay"`). A consumer reaches it via the relay with
`relay.forward{to,inner}` (single response) or `relay.stream{to,inner}`
(multiple frames). The relay only ever forwards ciphertext.

## 10. End-to-end encryption

Byte-compatible with NaCl `crypto_box`: X25519 + XSalsa20-Poly1305. The shared
key is derived from the two parties' Ed25519 identities mapped to X25519
(no handshake). Sealed form: `{"n":"<hex24 nonce>","c":"<hex ciphertext>"}`.

## 11. Built-in tasks

`hash.sha256`, `math.matmul`, `text.wordcount`, `data.aggregate`,
`crypto.pbkdf2`, `compress.gzip`, `sci.matmul` (numpy-gated), `ai.generate`,
`ai.embed`, `ai.layer`, `ai.shard`. Each maps `items → results` of equal
length. `ai.shard{spec,start,end}` runs a transformer layer range over hidden
states and is the basis of sharded big-model inference; all tasks except
`ai.generate` are deterministic and cacheable.

## 12. Constants

| Name | Default |
|---|---|
| `POW_DIFFICULTY_BITS` | 12 |
| `GENESIS_CREDITS` | 100 |
| `MAX_CLOCK_SKEW` | 300 s |
| DHT `K` / `ALPHA` | 8 / 3 |
| `MAX_MESSAGE_BYTES` | 32 MiB |
| Witness committee size / quorum | 5 / 2 (adaptive) |
