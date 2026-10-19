"""Network interface discovery and throughput collection."""

import time
from pathlib import Path

from ..config import BYTES_TO_MB


class NetworkCollector:
    """Discovers network interfaces and collects per-interface throughput."""

    def __init__(self) -> None:
        self._prev: dict[str, tuple] = {}

    @staticmethod
    def discover_interfaces() -> list[str]:
        """Discover physical / bridge / bond network interfaces (skip lo, veth, tap, fwbr, fwpr)."""
        try:
            lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
        except OSError:
            return []
        skip_prefixes = ("lo", "veth", "tap", "fwbr", "fwpr", "virbr", "docker")
        ifaces = []
        for line in lines:
            iface = line.split(":")[0].strip()
            if any(iface.startswith(p) for p in skip_prefixes):
                continue
            if iface:
                ifaces.append(iface)
        return sorted(ifaces)

    def throughput(self) -> dict:
        """Compute per-interface bytes/sec from /proc/net/dev delta."""
        result: dict = {}
        now = time.monotonic_ns()
        try:
            lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]  # skip headers
        except OSError:
            return result
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            rx_bytes, tx_bytes = int(parts[1]), int(parts[9])
            prev = self._prev.get(iface)
            self._prev[iface] = (rx_bytes, tx_bytes, now)
            if prev is None:
                continue
            prx, ptx, pt = prev
            dt = (now - pt) / 1e9  # seconds
            if dt <= 0:
                continue
            result[iface] = {
                "rx_mb_sec": round((rx_bytes - prx) / dt * BYTES_TO_MB, 2),
                "tx_mb_sec": round((tx_bytes - ptx) / dt * BYTES_TO_MB, 2),
            }
        return result
