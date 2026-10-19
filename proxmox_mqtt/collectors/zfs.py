"""ZFS pool discovery and status."""

import logging
import subprocess

from ..config import BYTES_TO_GB

LOG = logging.getLogger(__name__)


def discover_pools() -> list[str]:
    """Discover all ZFS pools via zpool list."""
    try:
        out = subprocess.run(
            ["zpool", "list", "-Hp", "-o", "name"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return []
        return [line.strip() for line in out.stdout.strip().splitlines() if line.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def pool_status(pool_name: str) -> dict | None:
    """Get ZFS pool usage percentage and health in a single zpool call."""
    try:
        out = subprocess.run(
            ["zpool", "list", "-Hp", "-o", "name,size,alloc,health", pool_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            LOG.warning("zpool list failed: %s", out.stderr.strip())
            return None
        cols = out.stdout.strip().split("\t")
        size, alloc = int(cols[1]), int(cols[2])
        health = cols[3] if len(cols) > 3 else None
        return {
            "used_pct": round(alloc / size * 100, 1) if size else 0,
            "total_gb": round(size * BYTES_TO_GB, 2),
            "used_gb": round(alloc * BYTES_TO_GB, 2),
            "avail_gb": round((size - alloc) * BYTES_TO_GB, 2),
            "health": health,
        }
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        LOG.exception("ZFS read failed for %s", pool_name)
        return None
