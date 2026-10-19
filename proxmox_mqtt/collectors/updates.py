"""Apt update information reader."""

import json
import logging
from pathlib import Path

LOG = logging.getLogger(__name__)

# Written by pveupdate / PVE::API2::APT — a JSON array of package dicts.
_PKGUPDATES = Path("/var/lib/pve-manager/pkgupdates")


def _from_pkgupdates() -> tuple[int, int] | None:
    """Read pending update counts from Proxmox's own cache.

    ``pveupdate`` runs daily (via cron) and writes the list of
    upgradable packages to ``/var/lib/pve-manager/pkgupdates``.
    Each entry has an ``Origin`` field — security updates come from
    origins containing ``security`` (case-insensitive).

    Returns ``(total, security)`` or ``None`` if the file is missing
    or unparseable.
    """
    try:
        text = _PKGUPDATES.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        pkgs = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        LOG.warning("Failed to parse %s", _PKGUPDATES)
        return None

    if not isinstance(pkgs, list):
        return None

    total = len(pkgs)
    security = sum(
        1 for p in pkgs
        if "security" in p.get("Origin", "").lower()
    )
    return total, security


def pending() -> dict:
    """Return a dict with reboot status and pending update counts.

    Reads the Proxmox ``pkgupdates`` cache written by ``pveupdate``.
    This is a pure file read — no subprocess, no network, no disk churn.
    """
    reboot = Path("/run/reboot-required").exists()

    counts = _from_pkgupdates()
    if counts is None:
        counts = (0, 0)
    total, security = counts

    return {
        "reboot_required": "on" if reboot else "off",
        "updates_available": total,
        "security_updates": security,
    }
