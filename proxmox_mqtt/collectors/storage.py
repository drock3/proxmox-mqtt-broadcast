"""Proxmox storage discovery and status.

Uses pvesm only once at startup to discover storages and their types.
Subsequent polls use native methods (statvfs, lvs) that don't wake
rotational drives — unlike pvesm status which queries ZFS pool metadata.
"""

import logging
import os
import re
import subprocess
from pathlib import Path

LOG = logging.getLogger(__name__)

_pvesm_warned = False


def discover() -> list[dict]:
    """Discover all Proxmox storage via pvesm status.

    Called once at startup and on rediscover.  The returned dicts include
    a ``poll_method`` key (``"statvfs"``, ``"lvmthin"``, or ``None``)
    and method-specific fields so that ``poll()`` can refresh values
    without calling pvesm again.
    """
    global _pvesm_warned  # noqa: PLW0603
    try:
        out = subprocess.run(
            ["pvesm", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            first_line = out.stderr.strip().splitlines()[0] if out.stderr.strip() else "unknown error"
            if not _pvesm_warned:
                LOG.warning("pvesm status failed: %s", first_line)
                _pvesm_warned = True
            else:
                LOG.debug("pvesm status still failing: %s", first_line)
            return []
        if _pvesm_warned:
            LOG.info("pvesm status recovered")
            _pvesm_warned = False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        LOG.warning("pvesm not found or timed out")
        return []

    # Parse /etc/pve/storage.cfg for paths and LVM details
    storage_cfg = _parse_storage_cfg()

    storages: list[dict] = []
    for line in out.stdout.strip().splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 7:
            continue
        name, stype, status = parts[0], parts[1], parts[2]
        try:
            total, used, avail = int(parts[3]), int(parts[4]), int(parts[5])
        except ValueError:
            continue
        pct = float(parts[6].rstrip("%")) if parts[6] != "-" else 0.0

        entry: dict = {
            "name": name,
            "type": stype,
            "status": status,
            "total": total,
            "used": used,
            "available": avail,
            "used_pct": pct,
            "poll_method": None,
        }

        cfg = storage_cfg.get(name, {})
        if stype in ("dir", "btrfs"):
            mount_path = cfg.get("path")
            if mount_path and Path(mount_path).is_dir():
                entry["poll_method"] = "statvfs"
                entry["mount_path"] = mount_path
        elif stype in ("nfs", "cifs", "glusterfs"):
            # Network mounts: pvesm mounts under /mnt/pve/<name>
            mount_path = cfg.get("path") or f"/mnt/pve/{name}"
            if Path(mount_path).is_dir():
                entry["poll_method"] = "statvfs"
                entry["mount_path"] = mount_path
        elif stype == "lvmthin":
            vgname = cfg.get("vgname")
            thinpool = cfg.get("thinpool")
            if vgname and thinpool:
                entry["poll_method"] = "lvmthin"
                entry["vgname"] = vgname
                entry["thinpool"] = thinpool
        elif stype == "lvm":
            vgname = cfg.get("vgname")
            if vgname:
                entry["poll_method"] = "lvm"
                entry["vgname"] = vgname
        # zfspool — skipped by bridge (handled by ZFS collector)
        # cephfs, rbd, iscsi — no native method, keep startup values

        storages.append(entry)
    return storages


def poll(storages: list[dict]) -> list[dict]:
    """Refresh storage stats using native methods (no pvesm).

    Updates total/used/available/used_pct in-place and returns the list.
    Storages without a poll_method keep their cached startup values.
    """
    for s in storages:
        method = s.get("poll_method")
        if method == "statvfs":
            _poll_statvfs(s)
        elif method == "lvmthin":
            _poll_lvmthin(s)
        elif method == "lvm":
            _poll_lvm(s)
    return storages


def _poll_statvfs(s: dict) -> None:
    """Update dir-type storage stats via os.statvfs (no subprocess, no disk I/O)."""
    try:
        st = os.statvfs(s["mount_path"])
        block_kib = st.f_frsize / 1024
        total_kib = int(st.f_blocks * block_kib)
        avail_kib = int(st.f_bavail * block_kib)
        used_kib = total_kib - avail_kib
        s["total"] = total_kib
        s["used"] = used_kib
        s["available"] = avail_kib
        s["used_pct"] = round(used_kib / total_kib * 100, 2) if total_kib else 0.0
        s["status"] = "active"
    except OSError:
        LOG.debug("statvfs failed for %s (%s)", s["name"], s.get("mount_path"))


def _poll_lvmthin(s: dict) -> None:
    """Update lvmthin storage stats via lvs."""
    try:
        out = subprocess.run(
            [
                "lvs", "--noheadings", "--nosuffix", "--units", "b",
                "-o", "lv_size,data_percent",
                f"{s['vgname']}/{s['thinpool']}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return
        parts = out.stdout.strip().split()
        if len(parts) < 2:
            return
        size_bytes = int(float(parts[0]))
        data_pct = float(parts[1])
        total_kib = size_bytes // 1024
        used_kib = int(total_kib * data_pct / 100)
        avail_kib = total_kib - used_kib
        s["total"] = total_kib
        s["used"] = used_kib
        s["available"] = avail_kib
        s["used_pct"] = round(data_pct, 2)
        s["status"] = "active"
    except (OSError, ValueError, subprocess.SubprocessError):
        LOG.debug("lvs failed for %s/%s", s.get("vgname"), s.get("thinpool"))


def _poll_lvm(s: dict) -> None:
    """Update thick-LVM storage stats via vgs."""
    try:
        out = subprocess.run(
            [
                "vgs", "--noheadings", "--nosuffix", "--units", "b",
                "-o", "vg_size,vg_free",
                s["vgname"],
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return
        parts = out.stdout.strip().split()
        if len(parts) < 2:
            return
        size_bytes = int(float(parts[0]))
        free_bytes = int(float(parts[1]))
        total_kib = size_bytes // 1024
        avail_kib = free_bytes // 1024
        used_kib = total_kib - avail_kib
        s["total"] = total_kib
        s["used"] = used_kib
        s["available"] = avail_kib
        s["used_pct"] = round(used_kib / total_kib * 100, 2) if total_kib else 0.0
        s["status"] = "active"
    except (OSError, ValueError, subprocess.SubprocessError):
        LOG.debug("vgs failed for %s", s.get("vgname"))


def _parse_storage_cfg() -> dict[str, dict]:
    """Parse /etc/pve/storage.cfg into {name: {key: value, ...}}."""
    result: dict[str, dict] = {}
    current: dict | None = None
    try:
        for line in Path("/etc/pve/storage.cfg").read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Section header: "type: name"
            m = re.match(r"^(\w+):\s+(\S+)", stripped)
            if m:
                name = m.group(2)
                current = {"_type": m.group(1)}
                result[name] = current
                continue
            # Key-value inside a section
            if current is not None and line[0] in (" ", "\t"):
                kv = stripped.split(None, 1)
                if len(kv) == 2:
                    current[kv[0]] = kv[1]
                elif len(kv) == 1:
                    current[kv[0]] = ""
    except OSError:
        LOG.warning("Cannot read /etc/pve/storage.cfg")
    return result
