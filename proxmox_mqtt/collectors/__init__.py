"""Local metric collector — facade composing domain-specific collectors."""

from .disks import DiskCollector
from .gpu import GpuCollector
from .guests import GuestCollector
from .network import NetworkCollector
from .node import NodeCollector
from .storage import discover as _discover_storage
from .storage import poll as _poll_storage
from .updates import pending as _pending_updates
from .zfs import discover_pools as _discover_zfs_pools
from .zfs import pool_status as _zfs_pool_status


class LocalCollector:
    """Collects host and guest metrics from the local filesystem.

    Reads CPU, memory, swap, temperatures, fan speeds, network throughput,
    disk I/O, SMART data, ZFS pool status, and Proxmox storage directly
    from procfs, sysfs, cgroups, and CLI tools — no API token required.

    This is a facade that delegates to domain-specific collectors.
    """

    def __init__(self, node: str) -> None:
        self.node = node
        self._node = NodeCollector()
        self._guests = GuestCollector(node)
        self._disks = DiskCollector()
        self._gpu = GpuCollector()
        self._network = NetworkCollector()

    # --- Auto-discovery (static) ---------------------------------------

    @staticmethod
    def discover_guests() -> list[dict]:
        """Discover all QEMU VMs and LXC containers from /etc/pve."""
        return GuestCollector.discover()

    @staticmethod
    def discover_zfs_pools() -> list[str]:
        """Discover all ZFS pools via zpool list."""
        return _discover_zfs_pools()

    @staticmethod
    def discover_storage() -> list[dict]:
        """Discover all Proxmox storage via pvesm status."""
        return _discover_storage()

    @staticmethod
    def discover_interfaces() -> list[str]:
        """Discover physical / bridge / bond network interfaces."""
        return NetworkCollector.discover_interfaces()

    def discover_disks(self) -> list[dict]:
        """Discover physical disks with model, serial, type, temp."""
        return self._disks.discover()

    @staticmethod
    def discover_gpus() -> list[dict]:
        """Discover all GPUs (Intel, AMD, NVIDIA)."""
        return GpuCollector.discover()

    def refresh_disk_status(self, disk: dict) -> dict:
        """Refresh volatile disk fields (temperature, health, standby)."""
        return self._disks.refresh_status(disk)

    # --- Node metrics --------------------------------------------------

    def node_status(self) -> dict:
        """Collect all node-level metrics (CPU, memory, swap, uptime, load, freq)."""
        return self._node.status()

    def node_temperatures(self) -> dict:
        """Read CPU and other temps from thermal zones and hwmon."""
        return self._node.temperatures()

    def fan_speeds(self) -> dict:
        """Read fan RPMs from /sys/class/hwmon."""
        return self._node.fan_speeds()

    # --- Network -------------------------------------------------------

    def network_throughput(self) -> dict:
        """Compute per-interface bytes/sec from /proc/net/dev delta."""
        return self._network.throughput()

    # --- Disk I/O ------------------------------------------------------

    def disk_io(self) -> dict:
        """Compute per-block-device read/write bytes/sec."""
        return self._disks.io()

    # --- Guests --------------------------------------------------------

    def guest_status(self, guest_type: str, vmid: int) -> dict | None:
        """Return status dict for a guest, or None if not found."""
        return self._guests.status(guest_type, vmid)

    def guest_filesystems(self, guest_type: str, vmid: int) -> list[dict]:
        """Return mount point filesystem usage for a running guest."""
        return self._guests.filesystems(guest_type, vmid)

    # --- ZFS -----------------------------------------------------------

    @staticmethod
    def zfs_pool(pool_name: str) -> dict | None:
        """Get ZFS pool usage percentage and health."""
        return _zfs_pool_status(pool_name)

    # --- Storage -------------------------------------------------------

    @staticmethod
    def storage_status() -> list[dict]:
        """Get current status of all Proxmox storage via pvesm."""
        return _discover_storage()

    @staticmethod
    def poll_storage(storages: list[dict]) -> list[dict]:
        """Refresh storage stats using native methods (no pvesm)."""
        return _poll_storage(storages)

    # --- GPUs ----------------------------------------------------------

    def gpu_stats(self, gpus: list[dict]) -> dict[str, dict]:
        """Collect live stats for all discovered GPUs."""
        return self._gpu.stats(gpus)

    # --- Updates -------------------------------------------------------

    @staticmethod
    def pending_updates() -> dict:
        """Read cached apt update info from the local filesystem."""
        return _pending_updates()
