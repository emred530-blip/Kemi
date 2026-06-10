"""Tracker: peer discovery and credit ledger service.

Like a BitTorrent tracker, this is the swarm's bootstrap point: providers
register themselves and heartbeat, consumers ask for the active provider
list. It additionally hosts the credit ledger (escrow-based payments).

The tracker never sees task payloads or results - chunks flow directly
between consumer and provider.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .identity import verify_token
from .ledger import Ledger, LedgerError
from .protocol import ProtocolError, error, ok, read_message, send_message

log = logging.getLogger("kemi.tracker")

# A provider that has not heartbeat for this long is dropped from listings.
PROVIDER_TTL = 30.0


class Tracker:
    def __init__(self, host: str = "0.0.0.0", port: int = 7700, db_path: str = ":memory:"):
        self.host = host
        self.port = port
        self.ledger = Ledger(db_path)
        self.providers: dict[str, dict[str, Any]] = {}
        self._server: asyncio.Server | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        log.info("tracker listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.ledger.close()

    async def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        async with self._server:
            await self._server.serve_forever()

    # -- request handling ----------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            message = await asyncio.wait_for(read_message(reader), timeout=30.0)
            response = self._dispatch(message)
        except (ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError) as exc:
            response = error("protocol_error", str(exc))
        except Exception:
            log.exception("unhandled error in tracker request")
            response = error("internal_error")
        try:
            await send_message(writer, response)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        msg_type = message.get("type")
        handler = {
            "register": self._on_register,
            "heartbeat": self._on_heartbeat,
            "unregister": self._on_unregister,
            "providers.list": self._on_providers_list,
            "balance": self._on_balance,
            "escrow.create": self._on_escrow_create,
            "escrow.redeem": self._on_escrow_redeem,
            "escrow.release": self._on_escrow_release,
        }.get(msg_type)
        if handler is None:
            return error("unknown_type", f"unknown message type: {msg_type!r}")
        try:
            return handler(message)
        except LedgerError as exc:
            return error(exc.code, str(exc))

    def _authenticate(self, message: dict[str, Any]) -> str:
        node_id = message.get("node_id", "")
        token = message.get("token", "")
        if not node_id or not token or not verify_token(node_id, token):
            raise LedgerError("auth_failed", "node_id/token mismatch")
        return node_id

    # -- peer discovery ------------------------------------------------------

    def _on_register(self, message: dict[str, Any]) -> dict[str, Any]:
        node_id = self._authenticate(message)
        balance = self.ledger.ensure_account(node_id)
        role = message.get("role", "consumer")
        if role == "provider":
            self.providers[node_id] = {
                "node_id": node_id,
                "host": message["host"],
                "port": int(message["port"]),
                "price": float(message.get("price", 1.0)),
                "tasks": list(message.get("tasks", [])),
                "resources": dict(message.get("resources", {})),
                "load": 0,
                "last_seen": time.monotonic(),
            }
            log.info("provider %s registered at %s:%s", node_id[:12], message["host"], message["port"])
        return ok(balance=balance)

    def _on_heartbeat(self, message: dict[str, Any]) -> dict[str, Any]:
        node_id = self._authenticate(message)
        provider = self.providers.get(node_id)
        if provider is None:
            return error("not_registered", "register as a provider first")
        provider["last_seen"] = time.monotonic()
        provider["load"] = int(message.get("load", 0))
        return ok()

    def _on_unregister(self, message: dict[str, Any]) -> dict[str, Any]:
        node_id = self._authenticate(message)
        self.providers.pop(node_id, None)
        return ok()

    def _on_providers_list(self, message: dict[str, Any]) -> dict[str, Any]:
        now = time.monotonic()
        stale = [nid for nid, p in self.providers.items() if now - p["last_seen"] > PROVIDER_TTL]
        for nid in stale:
            del self.providers[nid]
        listing = [
            {key: value for key, value in p.items() if key != "last_seen"}
            for p in self.providers.values()
        ]
        return ok(providers=listing)

    # -- ledger --------------------------------------------------------------

    def _on_balance(self, message: dict[str, Any]) -> dict[str, Any]:
        node_id = self._authenticate(message)
        return ok(balance=self.ledger.ensure_account(node_id))

    def _on_escrow_create(self, message: dict[str, Any]) -> dict[str, Any]:
        node_id = self._authenticate(message)
        self.ledger.ensure_account(node_id)
        escrow_id, redeem_key = self.ledger.escrow_create(node_id, float(message["amount"]))
        return ok(escrow_id=escrow_id, redeem_key=redeem_key)

    def _on_escrow_redeem(self, message: dict[str, Any]) -> dict[str, Any]:
        node_id = self._authenticate(message)
        balance = self.ledger.escrow_redeem(
            escrow_id=str(message["escrow_id"]),
            redeem_key=str(message["redeem_key"]),
            payee=node_id,
            amount=float(message["amount"]),
        )
        return ok(balance=balance)

    def _on_escrow_release(self, message: dict[str, Any]) -> dict[str, Any]:
        node_id = self._authenticate(message)
        refunded = self.ledger.escrow_release(str(message["escrow_id"]), node_id)
        return ok(refunded=refunded)
