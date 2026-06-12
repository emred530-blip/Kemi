"""Resource guards: token buckets and per-IP limits.

A public peer is an open TCP/UDP service; without limits a single hostile
host can exhaust file descriptors, CPU or the DHT's memory. Every limit
fails *closed for the abuser and open for the fleet*: legitimate swarms
stay far below the defaults, and limits are per-source so one bad actor
cannot starve everyone else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Limits:
    """Knobs for a node's resource guards (defaults suit a public peer)."""

    max_connections: int = 512        # concurrent TCP connections, global
    per_ip_connections: int = 64      # concurrent TCP connections per source IP
    per_ip_rate: float = 80.0         # sustained TCP requests per second per IP
    per_ip_burst: float = 400.0       # short-burst allowance per IP
    udp_rate: float = 120.0           # sustained DHT datagrams per second per IP
    udp_burst: float = 600.0
    max_relay_sessions: int = 64      # NATed peers we will relay for
    per_ip_relay_sessions: int = 4
    dht_max_keys: int = 4096          # distinct keys this node will store


class TokenBucket:
    """Classic token bucket; ``take()`` is O(1) and allocation-free."""

    __slots__ = ("rate", "burst", "tokens", "stamp")

    def __init__(self, rate: float, burst: float):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.stamp = time.monotonic()

    def take(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


@dataclass
class _IPState:
    bucket: TokenBucket
    connections: int = 0
    last_seen: float = field(default_factory=time.monotonic)


class IPGuard:
    """Per-source-IP rate and concurrency accounting with idle eviction."""

    def __init__(self, rate: float, burst: float, max_concurrent: int,
                 max_tracked: int = 4096):
        self.rate = rate
        self.burst = burst
        self.max_concurrent = max_concurrent
        self.max_tracked = max_tracked
        self._ips: dict[str, _IPState] = {}

    def _state(self, ip: str) -> _IPState:
        state = self._ips.get(ip)
        if state is None:
            if len(self._ips) >= self.max_tracked:
                self._evict()
            state = self._ips[ip] = _IPState(TokenBucket(self.rate, self.burst))
        state.last_seen = time.monotonic()
        return state

    def _evict(self) -> None:
        # Drop the longest-idle entries that hold no open connections.
        idle = sorted((s.last_seen, ip) for ip, s in self._ips.items()
                      if s.connections == 0)
        for _, ip in idle[:max(1, len(idle) // 4)]:
            del self._ips[ip]

    def allow_request(self, ip: str) -> bool:
        return self._state(ip).bucket.take()

    def try_connect(self, ip: str) -> bool:
        state = self._state(ip)
        if state.connections >= self.max_concurrent:
            return False
        state.connections += 1
        return True

    def disconnect(self, ip: str) -> None:
        state = self._ips.get(ip)
        if state is not None and state.connections > 0:
            state.connections -= 1

    def connections(self, ip: str) -> int:
        state = self._ips.get(ip)
        return state.connections if state else 0
