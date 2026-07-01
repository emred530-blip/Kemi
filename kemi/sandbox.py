"""Sandboxed task execution: subprocess isolation with OS resource limits.

Even though tasks are allowlisted (never arbitrary code), defence in depth
matters when running other people's workloads: each chunk runs in a fresh
child process with hard caps on CPU seconds, address space and file
descriptors, in its own session, so a runaway or exploited task cannot take
the provider down with it.

Tasks that need long-lived state (the ``ai.*`` family keeps a loaded model
in memory) opt out via ``UNSANDBOXED_TASKS`` and run in-process instead.
(Roadmap: container/WASM isolation with filesystem and network namespaces.)
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from typing import Any

# Backend-bound tasks that must run in the provider process.
UNSANDBOXED_TASKS = frozenset({"ai.generate", "ai.embed"})

DEFAULT_MEM_MB = 512


class SandboxError(Exception):
    pass


async def run_sandboxed(
    task: str,
    items: list[Any],
    params: dict[str, Any],
    timeout: float,
    mem_mb: int = DEFAULT_MEM_MB,
) -> list[Any]:
    """Execute an allowlisted task in a resource-limited child process."""
    request = json.dumps({
        "task": task,
        "items": items,
        "params": params,
        "cpu_seconds": math.ceil(timeout),
        "mem_mb": mem_mb,
    }).encode("utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "kemi.sandbox",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(request), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise SandboxError(f"chunk exceeded {timeout}s wall clock")
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        raise SandboxError(detail[-1] if detail else f"sandbox exited {process.returncode}")
    try:
        response = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxError(f"sandbox produced invalid output: {exc}")
    if not response.get("ok"):
        raise SandboxError(response.get("error", "unknown sandbox failure"))
    return response["results"]


def _child_main() -> int:  # pragma: no cover - runs in a subprocess
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        import resource

        cpu = int(request["cpu_seconds"])
        mem = int(request["mem_mb"]) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass  # platform without rlimits: wall-clock timeout still applies

    from .tasks import TaskError, run_task

    try:
        results = run_task(request["task"], request["items"], request["params"], context={})
        payload = {"ok": True, "results": results}
    except (TaskError, MemoryError) as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_child_main())
