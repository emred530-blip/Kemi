"""Provider node: rents out local compute capacity for credits.

The provider registers with a tracker, advertises its resources, price and
supported task types, then serves ``task.execute`` requests directly from
consumers. Each completed chunk is paid for by redeeming part of the
consumer's escrow at the tracker before the result is returned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
from typing import Any

from .ai_backends import AIBackend, load_backend
from .identity import Identity
from .protocol import ProtocolError, error, ok, read_message, request, send_message
from .tasks import TASKS, TaskError, run_task

log = logging.getLogger("kemi.provider")

HEARTBEAT_INTERVAL = 10.0


def detect_resources() -> dict[str, Any]:
    resources: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 1,
        "machine": platform.machine(),
        "system": platform.system(),
    }
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    resources["mem_total_mb"] = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    return resources


class ProviderNode:
    def __init__(
        self,
        identity: Identity,
        tracker_host: str,
        tracker_port: int,
        host: str = "0.0.0.0",
        port: int = 0,
        advertise_host: str | None = None,
        price: float = 1.0,
        max_workers: int | None = None,
        task_timeout: float = 300.0,
        ai_backend: str | AIBackend | None = "mock",
    ):
        self.identity = identity
        self.tracker_host = tracker_host
        self.tracker_port = tracker_port
        self.host = host
        self.port = port
        self.advertise_host = advertise_host or ("127.0.0.1" if host in ("0.0.0.0", "") else host)
        self.price = price
        self.max_workers = max_workers or (os.cpu_count() or 1)
        self.task_timeout = task_timeout
        self.resources = detect_resources()
        if isinstance(ai_backend, str):
            ai_backend = load_backend(ai_backend)
        self._context: dict[str, Any] = {"ai_backend": ai_backend}
        self._semaphore = asyncio.Semaphore(self.max_workers)
        self._active_tasks = 0
        self._server: asyncio.Server | None = None
        self._heartbeat_task: asyncio.Task | None = None

    @property
    def supported_tasks(self) -> list[str]:
        names = list(TASKS)
        if self._context.get("ai_backend") is None:
            names = [n for n in names if not n.startswith("ai.")]
        return names

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        await self._register()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        log.info(
            "provider %s serving on %s:%s (%s workers, %.2f credits/item)",
            self.identity.short_id, self.advertise_host, self.port, self.max_workers, self.price,
        )

    async def stop(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        try:
            await self._tracker_request({"type": "unregister"})
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        async with self._server:
            await self._server.serve_forever()

    # -- tracker interaction -------------------------------------------------

    async def _tracker_request(self, message: dict[str, Any]) -> dict[str, Any]:
        message = {
            **message,
            "node_id": self.identity.node_id,
            "token": self.identity.token,
        }
        return await request(self.tracker_host, self.tracker_port, message)

    async def _register(self) -> None:
        response = await self._tracker_request(
            {
                "type": "register",
                "role": "provider",
                "host": self.advertise_host,
                "port": self.port,
                "price": self.price,
                "tasks": self.supported_tasks,
                "resources": self.resources,
            }
        )
        if not response.get("ok"):
            raise RuntimeError(f"tracker registration failed: {response}")
        log.info("registered with tracker, balance=%.2f", response.get("balance", 0.0))

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self._tracker_request({"type": "heartbeat", "load": self._active_tasks})
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                log.warning("heartbeat failed: %s", exc)

    # -- consumer-facing server ----------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            message = await asyncio.wait_for(read_message(reader), timeout=60.0)
            response = await self._dispatch(message)
        except (ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError) as exc:
            response = error("protocol_error", str(exc))
        except Exception:
            log.exception("unhandled error in provider request")
            response = error("internal_error")
        try:
            await send_message(writer, response)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        msg_type = message.get("type")
        if msg_type == "node.info":
            return ok(
                node_id=self.identity.node_id,
                price=self.price,
                tasks=self.supported_tasks,
                resources=self.resources,
                load=self._active_tasks,
            )
        if msg_type == "task.execute":
            return await self._on_task_execute(message)
        return error("unknown_type", f"unknown message type: {msg_type!r}")

    async def _on_task_execute(self, message: dict[str, Any]) -> dict[str, Any]:
        task = str(message.get("task", ""))
        items = message.get("items")
        params = dict(message.get("params") or {})
        payment = dict(message.get("payment") or {})
        if task not in self.supported_tasks:
            return error("unsupported_task", f"task {task!r} not offered by this provider")
        if not isinstance(items, list) or not items:
            return error("bad_request", "items must be a non-empty list")

        amount = round(self.price * len(items), 6)
        async with self._semaphore:
            self._active_tasks += 1
            try:
                results = await asyncio.wait_for(
                    asyncio.to_thread(run_task, task, items, params, self._context),
                    timeout=self.task_timeout,
                )
            except TaskError as exc:
                return error("task_failed", str(exc))
            except asyncio.TimeoutError:
                return error("task_timeout", f"chunk exceeded {self.task_timeout}s")
            finally:
                self._active_tasks -= 1

        # Payment before result: redeem our share of the consumer's escrow.
        # If redemption fails the consumer does not get the result.
        try:
            redeem = await self._tracker_request(
                {
                    "type": "escrow.redeem",
                    "escrow_id": payment.get("escrow_id", ""),
                    "redeem_key": payment.get("redeem_key", ""),
                    "amount": amount,
                }
            )
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            return error("payment_failed", f"tracker unreachable: {exc}")
        if not redeem.get("ok"):
            return error("payment_failed", f"{redeem.get('error')}: {redeem.get('detail', '')}")

        return ok(results=results, charged=amount)
