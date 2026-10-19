"""Local metric collector — reads directly from procfs / cgroups / zpool."""

import json
import logging
import os
import re
import signal
import socket as _socket
import subprocess
import tempfile
import time
from pathlib import Path

from .config import BYTES_TO_GB, BYTES_TO_MB, CGROUP_ROOT_PATH

LOG = logging.getLogger(__name__)

CGROUP_ROOT = Path(CGROUP_ROOT_PATH)

_pvesm_warned = False          # rate-limit repeated pvesm warnings


class LocalCollector:
    """Collects host and guest metrics from the local filesystem.

    Reads CPU, memory, swap, temperatures, fan speeds, network throughput,
    disk I/O, SMART data, ZFS pool status, and Proxmox storage directly
    from procfs, sysfs, cgroups, and CLI tools — no API token required.
    """

    def __init__(self, node: str) -> None:
        self.node = node
        self._cgroup_v2 = (CGROUP_ROOT / "cgroup.controllers").exists()
        self._prev_node_cpu: tuple[int, int] | None = None
        self._prev_guest_cpu: dict[tuple, tuple] = {}
        self._prev_net: dict[str, tuple] = {}
        self._prev_diskio: dict[str, tuple] = {}

    # --- Auto-discovery ------------------------------------------------

    @staticmethod
    def discover_guests() -> list[dict]:
        """Discover all QEMU VMs and LXC containers from /etc/pve."""
        guests: list[dict] = []
        cli_names = LocalCollector._guest_names_from_cli()
        for guest_type, conf_dir in (("qemu", "/etc/pve/qemu-server"), ("lxc", "/etc/pve/lxc")):
            d = Path(conf_dir)
            if not d.is_dir():
                continue
            for conf in sorted(d.glob("*.conf")):
                try:
                    vmid = int(conf.stem)
                except ValueError:
                    continue
                name = str(vmid)
                try:
                    for line in conf.read_text(encoding="utf-8").splitlines():
                        stripped = line.strip()
                        if stripped.startswith("hostname:") or stripped.startswith("name:"):
                            name = stripped.split(":", 1)[1].strip()
                            break
                except PermissionError:
                    LOG.warning("Cannot read %s (permission denied)", conf)
                if name == str(vmid) and vmid in cli_names:
                    name = cli_names[vmid]
                guests.append({"vmid": vmid, "type": guest_type, "name": f"{name} ({vmid})"})
        return guests

    @staticmethod
    def _guest_names_from_cli() -> dict[int, str]:
        """Build a VMID → name map from qm list and pct list as fallback."""
        names: dict[int, str] = {}
        for cmd, name_idx in (
            (["qm", "list"], 1),   # VMID NAME STATUS ...
            (["pct", "list"], -1),  # VMID Status Lock Name
        ):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if out.returncode != 0:
                    continue
                for line in out.stdout.strip().splitlines()[1:]:  # skip header
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            vmid = int(parts[0])
                            name = parts[name_idx]
                            if name != str(vmid):
                                names[vmid] = name
                        except (ValueError, IndexError):
                            continue
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return names

    @staticmethod
    def discover_zfs_pools() -> list[str]:
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

    @staticmethod
    def discover_storage() -> list[dict]:
        """Discover all Proxmox storage via pvesm status."""
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
                storages.append({
                    "name": name,
                    "type": stype,
                    "status": status,
                    "total": total,
                    "used": used,
                    "available": avail,
                    "used_pct": pct,
                })
            return storages
        except (FileNotFoundError, subprocess.TimeoutExpired):
            LOG.warning("pvesm not found or timed out")
            return []

    @staticmethod
    def discover_disks() -> list[dict]:
        """Discover physical disks with model, serial, type, temp.

        Uses --nocheck and reads only already-cached attributes to AVOID
        spinning up sleeping disks.  If the disk is in standby/sleep the
        power mode query succeeds without spinup, and we skip SMART
        entirely — returning None for temperature and health.
        """
        disks: list[dict] = []
        try:
            lsblk = subprocess.run(
                ["lsblk", "-dnpo", "NAME,TYPE,ROTA,MODEL,SERIAL,SIZE"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if lsblk.returncode != 0:
                return disks
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return disks

        for line in lsblk.stdout.strip().splitlines():
            parts = line.split(None, 5)
            if len(parts) < 3:
                continue
            name, dtype, rota = parts[0], parts[1], parts[2]
            if dtype != "disk":
                continue
            dev = Path(name).name
            model = parts[3] if len(parts) > 3 else "Unknown"
            serial = parts[4] if len(parts) > 4 else "Unknown"
            size = parts[5].strip() if len(parts) > 5 else "Unknown"
            is_rotational = rota == "1"

            temp = None
            health = None
            standby = False

            if is_rotational:
                standby = LocalCollector._disk_is_standby(name)

            if not standby:
                temp = LocalCollector._disk_temperature(name, dev, rotational=is_rotational)
                health = LocalCollector._disk_health(name)

            disks.append({
                "dev": dev,
                "path": name,
                "model": model,
                "serial": serial,
                "size": size,
                "rotational": is_rotational,
                "temperature": temp,
                "health": health,
                "standby": standby,
            })
        return disks

    @staticmethod
    def refresh_disk_status(disk: dict) -> dict:
        """Refresh only volatile fields (temperature, health, standby) for a known disk.

        Avoids re-running lsblk or re-discovering static attributes.
        """
        path = disk["path"]
        dev = disk["dev"]
        is_rot = disk["rotational"]
        standby = False
        temp = None
        health = None

        if is_rot:
            standby = LocalCollector._disk_is_standby(path)

        if not standby:
            temp = LocalCollector._disk_temperature(path, dev, rotational=is_rot)
            if not is_rot:
                health = LocalCollector._disk_health(path)

        return {"temperature": temp, "health": health, "standby": standby}

    # --- Node ----------------------------------------------------------

    def node_status(self) -> dict:
        """Collect all node-level metrics (CPU, memory, swap, uptime, load, freq)."""
        mem_swap = self._node_mem_and_swap()
        return {
            "cpu": self._node_cpu(),
            **mem_swap,
            "uptime": self._node_uptime(),
            **self._node_loadavg(),
            "cpu_freq_mhz": self._node_cpu_freq(),
        }

    @staticmethod
    def pending_updates() -> dict:
        """Read cached apt update info from the local filesystem.

        Reads only pre-existing files — never runs apt, never hits the
        network, and never causes disk spinup.

        - ``/run/reboot-required`` lives on tmpfs (zero I/O).
        - ``/var/lib/update-notifier/updates-available`` is a tiny text
          file written by apt hooks (requires ``update-notifier-common``).
        """
        reboot = Path("/run/reboot-required").exists()
        total = 0
        security = 0

        updates_file = Path("/var/lib/update-notifier/updates-available")
        try:
            text = updates_file.read_text(encoding="utf-8")
            for line in text.splitlines():
                m = re.match(r"(\d+)", line.strip())
                if m:
                    count = int(m.group(1))
                    if "security" in line.lower():
                        security = count
                    elif "can be applied" in line.lower():
                        total = count
        except OSError:
            pass

        return {
            "reboot_required": "on" if reboot else "off",
            "updates_available": total,
            "security_updates": security,
        }

    def node_temperatures(self) -> dict:
        """Read CPU and other temps from thermal zones and hwmon."""
        temps: dict = {}
        for tz in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            try:
                temp = int((tz / "temp").read_text(encoding="utf-8").strip()) / 1000.0
                ttype = (tz / "type").read_text(encoding="utf-8").strip()
                temps[ttype] = round(temp, 1)
            except (OSError, ValueError):
                continue
        return temps

    def fan_speeds(self) -> dict:
        """Read fan RPMs from /sys/class/hwmon (fan*_input files)."""
        fans: dict = {}
        for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                hw_name = (hwmon / "name").read_text(encoding="utf-8").strip()
            except OSError:
                hw_name = hwmon.name
            for fan_input in sorted(hwmon.glob("fan*_input")):
                try:
                    rpm = int(fan_input.read_text(encoding="utf-8").strip())
                    fan_idx = fan_input.name.replace("_input", "")
                    label_file = hwmon / f"{fan_idx}_label"
                    if label_file.exists():
                        label = label_file.read_text(encoding="utf-8").strip()
                    else:
                        label = f"{hw_name}_{fan_idx}"
                    fans[label] = rpm
                except (OSError, ValueError):
                    continue
        return fans

    def network_throughput(self) -> dict:
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
            prev = self._prev_net.get(iface)
            self._prev_net[iface] = (rx_bytes, tx_bytes, now)
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

    def disk_io(self) -> dict:
        """Compute per-block-device read/write bytes/sec from /proc/diskstats.

        Only reports real devices (sd*, nvme*, vd*), skips partitions.
        """
        result: dict = {}
        now = time.monotonic_ns()
        try:
            lines = Path("/proc/diskstats").read_text(encoding="utf-8").splitlines()
        except OSError:
            return result
        for line in lines:
            parts = line.split()
            if len(parts) < 14:
                continue
            dev = parts[2]
            if not re.match(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$", dev):
                continue
            sectors_read, sectors_written = int(parts[5]), int(parts[9])
            read_bytes = sectors_read * 512
            write_bytes = sectors_written * 512
            prev = self._prev_diskio.get(dev)
            self._prev_diskio[dev] = (read_bytes, write_bytes, now)
            if prev is None:
                continue
            pr, pw, pt = prev
            dt = (now - pt) / 1e9
            if dt <= 0:
                continue
            result[dev] = {
                "read_mb_sec": round((read_bytes - pr) / dt * BYTES_TO_MB, 2),
                "write_mb_sec": round((write_bytes - pw) / dt * BYTES_TO_MB, 2),
            }
        return result

    # --- Guests (QEMU / LXC) ------------------------------------------

    def guest_status(self, guest_type: str, vmid: int) -> dict | None:
        """Return status dict for a guest, or None if not found."""
        # Prefer pvesh — returns the same values as the Proxmox UI
        pvesh = self._pvesh_guest_status(guest_type, vmid)
        if pvesh is not None:
            return pvesh

        # Fallback: cgroup-based collection
        cg = self._find_cgroup(guest_type, vmid)
        if cg is not None:
            cpu = self._cg_cpu(guest_type, vmid, cg)
            mem = self._cg_memory(cg)
            return {
                "status": "running",
                "cpu": cpu,
                "mem_used_gb": mem["used_gb"],
                "mem_used_pct": mem["used_pct"],
            }

        # Fallback: /proc-based for QEMU when cgroup is inaccessible
        if guest_type == "qemu":
            pid = self._find_qemu_pid(vmid)
            if pid is not None:
                return self._proc_guest_status(guest_type, vmid, pid)

        conf = self._pve_conf_path(guest_type, vmid)
        if conf.exists():
            return {"status": "stopped", "cpu": 0.0, "mem_used_gb": 0.0, "mem_used_pct": 0.0}
        return None

    def guest_filesystems(self, guest_type: str, vmid: int) -> list[dict]:
        """Return mount point filesystem usage for a running guest.

        For LXC containers, reads directly from procfs and uses statvfs.
        For QEMU VMs, queries the guest agent via ``qm agent get-fsinfo``.

        Returns:
            List of dicts with keys: mountpoint, fstype, total_gb,
            used_gb, available_gb, used_pct.
        """
        if guest_type == "lxc":
            return self._lxc_filesystems(vmid)
        if guest_type == "qemu":
            return self._qemu_filesystems(vmid)
        return []

    def _lxc_filesystems(self, vmid: int) -> list[dict]:
        """Read filesystem usage for an LXC container via host procfs."""
        cg = self._find_cgroup("lxc", vmid)
        if cg is None:
            return []
        pid = self._cg_container_pid(cg)
        if pid is None:
            LOG.debug("LXC %d: could not find any PID in cgroup tree", vmid)
            return []
        return self._filesystems_from_mountinfo(pid)

    @staticmethod
    def _qemu_filesystems(vmid: int) -> list[dict]:
        """Read filesystem usage for a QEMU VM via the guest agent."""
        try:
            out = subprocess.run(
                ["qm", "agent", str(vmid), "get-fsinfo"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode != 0:
                return []
            data = json.loads(out.stdout)
            if isinstance(data, dict) and "result" in data:
                data = data["result"]
            if not isinstance(data, list):
                return []
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, subprocess.SubprocessError):
            return []

        filesystems: list[dict] = []
        for fs in data:
            total = fs.get("total-bytes")
            used = fs.get("used-bytes")
            if total is None or used is None or total == 0:
                continue
            mount = fs.get("mountpoint", "")
            if not mount:
                continue
            avail = total - used
            filesystems.append({
                "mountpoint": mount,
                "fstype": fs.get("type", "unknown"),
                "total_gb": round(total * BYTES_TO_GB, 2),
                "used_gb": round(used * BYTES_TO_GB, 2),
                "available_gb": round(avail * BYTES_TO_GB, 2),
                "used_pct": round(used / total * 100, 1),
            })
        return filesystems

    # --- ZFS -----------------------------------------------------------

    @staticmethod
    def zfs_pool(pool_name: str) -> dict | None:
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

    @staticmethod
    def zfs_pool_health(pool_name: str) -> str | None:
        """Get ZFS pool health state (ONLINE, DEGRADED, FAULTED, etc.).

        Prefer calling zfs_pool() which returns health in the same call.
        """
        try:
            out = subprocess.run(
                ["zpool", "list", "-Hp", "-o", "health", pool_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode != 0:
                return None
            return out.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def storage_status() -> list[dict]:
        """Get current status of all Proxmox storage via pvesm."""
        return LocalCollector.discover_storage()

    # --- GPUs ----------------------------------------------------------

    @staticmethod
    def discover_gpus() -> list[dict]:
        """Discover all GPUs (Intel, AMD, NVIDIA) from sysfs DRM and nvidia-smi.

        Returns:
            List of dicts with keys: id, vendor, driver, name, and
            vendor-specific extras (sysfs_path or nvidia_index/uuid).
        """
        gpus: list[dict] = []
        drm = Path("/sys/class/drm")
        if drm.exists():
            for card in sorted(drm.glob("card[0-9]*")):
                driver_link = card / "device" / "driver"
                if not driver_link.is_symlink():
                    continue
                driver = driver_link.resolve().name
                if driver in ("i915", "xe"):
                    vendor = "intel"
                elif driver == "amdgpu":
                    vendor = "amd"
                elif driver == "nvidia":
                    # Handled by nvidia-smi below for richer stats
                    continue
                else:
                    continue
                name = LocalCollector._gpu_name_from_sysfs(card)
                gpus.append({
                    "id": card.name,
                    "vendor": vendor,
                    "driver": driver,
                    "name": name,
                    "sysfs_path": str(card),
                })

        gpus.extend(LocalCollector._discover_nvidia_gpus())
        return gpus

    @staticmethod
    def _gpu_name_from_sysfs(card: Path) -> str:
        """Resolve a human-readable GPU name via lspci, falling back to the card id."""
        try:
            uevent = (card / "device" / "uevent").read_text(encoding="utf-8")
            for line in uevent.splitlines():
                if line.startswith("PCI_SLOT_NAME="):
                    pci_slot = line.split("=", 1)[1]
                    out = subprocess.run(
                        ["lspci", "-s", pci_slot],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if out.returncode == 0 and out.stdout.strip():
                        return out.stdout.strip().split(": ", 1)[-1]
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass
        return f"GPU ({card.name})"

    @staticmethod
    def _discover_nvidia_gpus() -> list[dict]:
        """Discover NVIDIA GPUs via nvidia-smi."""
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,name,uuid", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode != 0:
                return []
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        gpus: list[dict] = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            gpus.append({
                "id": f"nvidia_{idx}",
                "vendor": "nvidia",
                "driver": "nvidia",
                "name": parts[1],
                "nvidia_index": idx,
                "uuid": parts[2],
            })
        return gpus

    def gpu_stats(self, gpus: list[dict]) -> dict[str, dict]:
        """Collect live stats for all discovered GPUs.

        Returns:
            Dict mapping GPU id to a stats dict with vendor-specific keys.
        """
        result: dict[str, dict] = {}

        nvidia_gpus = [g for g in gpus if g["vendor"] == "nvidia"]
        if nvidia_gpus:
            nvidia_data = self._nvidia_gpu_stats()
            for g in nvidia_gpus:
                idx = g["nvidia_index"]
                if idx in nvidia_data:
                    result[g["id"]] = nvidia_data[idx]

        for g in gpus:
            if g["vendor"] == "intel":
                result[g["id"]] = self._intel_gpu_stats(Path(g["sysfs_path"]), g["driver"])
            elif g["vendor"] == "amd":
                result[g["id"]] = self._amd_gpu_stats(Path(g["sysfs_path"]))

        return result

    @staticmethod
    def _nvidia_gpu_stats() -> dict[int, dict]:
        """Query all NVIDIA GPU stats via a single nvidia-smi call."""
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,utilization.gpu,memory.total,memory.used,"
                    "temperature.gpu,power.draw,fan.speed,clocks.current.graphics,"
                    "clocks.current.memory",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode != 0:
                return {}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {}

        def _to_float(val: str) -> float | None:
            try:
                return float(val)
            except ValueError:
                return None

        result: dict[int, dict] = {}
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 9:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            mem_total = _to_float(parts[2])
            mem_used = _to_float(parts[3])
            mem_pct = None
            if mem_total and mem_used is not None:
                mem_pct = round(mem_used / mem_total * 100, 1) if mem_total > 0 else 0.0
            result[idx] = {
                "utilization_pct": _to_float(parts[1]),
                "memory_total_mb": mem_total,
                "memory_used_mb": mem_used,
                "memory_used_pct": mem_pct,
                "temperature_c": _to_float(parts[4]),
                "power_w": _to_float(parts[5]),
                "fan_speed_pct": _to_float(parts[6]),
                "clock_mhz": _to_float(parts[7]),
                "memory_clock_mhz": _to_float(parts[8]),
            }
        return result

    @staticmethod
    def _intel_gpu_stats(card: Path, driver: str) -> dict:
        """Read Intel GPU stats from sysfs and intel_gpu_top.

        Sysfs provides clock frequencies; intel_gpu_top adds utilization,
        RC6 residency, power draw, and per-engine busy percentages.
        """
        stats: dict = {}
        if driver == "i915":
            for attr, key in (
                ("gt_cur_freq_mhz", "clock_mhz"),
                ("gt_max_freq_mhz", "clock_max_mhz"),
                ("gt_min_freq_mhz", "clock_min_mhz"),
            ):
                try:
                    stats[key] = float((card / attr).read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    pass
        elif driver == "xe":
            for gt_dir in sorted((card / "device" / "tile0").glob("gt*")):
                freq_dir = gt_dir / "freq0"
                if not freq_dir.exists():
                    continue
                for attr, key in (
                    ("cur_freq", "clock_mhz"),
                    ("max_freq", "clock_max_mhz"),
                    ("min_freq", "clock_min_mhz"),
                ):
                    try:
                        stats[key] = float((freq_dir / attr).read_text(encoding="utf-8").strip())
                    except (OSError, ValueError):
                        pass
                break  # first GT only

        # Merge live stats from intel_gpu_top (utilization, power, rc6, engines)
        pci_slot = LocalCollector._pci_slot_for_card(card)
        top_stats = LocalCollector._intel_gpu_top_stats(pci_slot)
        stats.update(top_stats)
        return stats

    @staticmethod
    def _pci_slot_for_card(card: Path) -> str | None:
        """Read the PCI slot address for a DRM card (e.g. '0000:00:02.0')."""
        try:
            uevent = (card / "device" / "uevent").read_text(encoding="utf-8")
            for line in uevent.splitlines():
                if line.startswith("PCI_SLOT_NAME="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return None

    @staticmethod
    def _intel_gpu_top_stats(pci_slot: str | None = None) -> dict:
        """Read Intel GPU utilization via intel_gpu_top (requires root or cap_perfmon).

        Runs intel_gpu_top in JSON mode for a brief sample and parses
        the output.  Pure perf-counter read — no disk I/O.
        """
        cmd = ["intel_gpu_top", "-J", "-s", "500"]
        if pci_slot:
            cmd.extend(["-d", f"pci:slot={pci_slot}"])

        # Write to a temp file instead of a pipe or PTY.
        #  - Pipe: glibc full-buffers; data never reaches us before kill.
        #  - PTY:  intel_gpu_top uses cursor repositioning per sample,
        #          corrupting the JSON when samples overlap on "screen".
        #  - File: after SIGINT the process flushes and exits cleanly,
        #          leaving a complete JSON file we can read.
        fd, tmppath = tempfile.mkstemp(suffix=".json", prefix="igt_")
        try:
            proc = subprocess.Popen(
                cmd, stdout=fd, stderr=subprocess.PIPE, close_fds=True,
            )
        except FileNotFoundError:
            os.close(fd)
            os.unlink(tmppath)
            LOG.warning("intel_gpu_top not found — install intel-gpu-tools")
            return {}

        os.close(fd)  # child owns the file descriptor now

        # Let it collect ~2 samples (500ms each), then ask it to stop.
        try:
            proc.wait(timeout=2.5)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        try:
            raw = Path(tmppath).read_text(encoding="utf-8")
        except OSError:
            raw = ""
        finally:
            try:
                os.unlink(tmppath)
            except OSError:
                pass

        if not raw:
            LOG.debug("intel_gpu_top returned no output (check CAP_PERFMON / video group)")
            return {}

        # intel_gpu_top -J emits a JSON array: [ <init>, <sample1>, ... ]
        # The process may be killed mid-stream, so the array is often truncated.
        # Strategy: trim to last complete object, close the array, and parse.
        trimmed = raw.rstrip()
        # Some versions of intel_gpu_top omit commas between top-level
        # objects when stdout is not a TTY.  Fix: insert missing commas
        # between adjacent "}\n{" (only appears at the array level).
        trimmed = trimmed.replace("}\n{", "},\n{")
        if trimmed.endswith(","):
            trimmed = trimmed[:-1]
        if not trimmed.endswith("]"):
            trimmed += "]"

        data_list: list[dict] = []
        try:
            data_list = json.loads(trimmed)
        except json.JSONDecodeError:
            # Truncated mid-object — trim the last partial object
            last_close = trimmed.rfind("},")
            if last_close > 0:
                try:
                    data_list = json.loads(trimmed[: last_close + 1] + "]")
                except json.JSONDecodeError:
                    return {}
            else:
                return {}

        if not isinstance(data_list, list):
            return {}

        # Pick the last sample with a real measurement (period > 100 ms)
        sample: dict | None = None
        for entry in reversed(data_list):
            duration = entry.get("period", {}).get("duration", 0)
            if duration >= 100:
                sample = entry
                break
        if sample is None:
            return {}

        stats: dict = {}

        # Overall utilization — max of all engine busy percentages
        engines = sample.get("engines", {})
        max_busy = 0.0
        for edata in engines.values():
            busy = edata.get("busy", 0.0)
            if busy > max_busy:
                max_busy = busy
        stats["utilization_pct"] = round(max_busy, 1)

        # Per-engine utilization (render, video, blitter, video_enhance)
        for ename, edata in engines.items():
            safe = re.sub(r"[^a-z0-9]+", "_", ename.lower()).strip("_")
            stats[f"engine_{safe}_pct"] = round(edata.get("busy", 0.0), 1)

        # RC6 residency (GPU idle %)
        rc6 = sample.get("rc6", {})
        if "value" in rc6:
            stats["rc6_pct"] = round(rc6["value"], 1)

        # Power
        power = sample.get("power", {})
        if "GPU" in power:
            stats["power_w"] = round(power["GPU"], 2)
        if "Package" in power:
            stats["power_package_w"] = round(power["Package"], 2)

        # IMC (memory controller) bandwidth
        imc = sample.get("imc-bandwidth", {})
        if "reads" in imc:
            stats["imc_reads_mib_s"] = round(imc["reads"], 2)
        if "writes" in imc:
            stats["imc_writes_mib_s"] = round(imc["writes"], 2)

        return stats

    @staticmethod
    def _amd_gpu_stats(card: Path) -> dict:
        """Read AMD GPU stats from sysfs (amdgpu driver)."""
        dev = card / "device"
        stats: dict = {}

        # GPU utilization
        try:
            stats["utilization_pct"] = float(
                (dev / "gpu_busy_percent").read_text(encoding="utf-8").strip(),
            )
        except (OSError, ValueError):
            pass

        # VRAM
        try:
            vram_total = int((dev / "mem_info_vram_total").read_text(encoding="utf-8").strip())
            vram_used = int((dev / "mem_info_vram_used").read_text(encoding="utf-8").strip())
            stats["memory_total_mb"] = round(vram_total / 1_048_576, 1)
            stats["memory_used_mb"] = round(vram_used / 1_048_576, 1)
            stats["memory_used_pct"] = round(vram_used / vram_total * 100, 1) if vram_total > 0 else 0.0
        except (OSError, ValueError):
            pass

        # Temperature and power from hwmon
        for hwmon in sorted((dev / "hwmon").glob("hwmon*")):
            if "temperature_c" not in stats:
                try:
                    stats["temperature_c"] = round(
                        int((hwmon / "temp1_input").read_text(encoding="utf-8").strip()) / 1000.0,
                        1,
                    )
                except (OSError, ValueError):
                    pass
            if "power_w" not in stats:
                try:
                    stats["power_w"] = round(
                        int((hwmon / "power1_average").read_text(encoding="utf-8").strip()) / 1_000_000.0,
                        1,
                    )
                except (OSError, ValueError):
                    pass
            break

        # GPU clock — active entry marked with * in pp_dpm_sclk
        try:
            for line in (dev / "pp_dpm_sclk").read_text(encoding="utf-8").splitlines():
                if "*" in line:
                    m = re.search(r"(\d+)\s*Mhz", line, re.IGNORECASE)
                    if m:
                        stats["clock_mhz"] = float(m.group(1))
                    break
        except OSError:
            pass

        # Memory clock — active entry marked with * in pp_dpm_mclk
        try:
            for line in (dev / "pp_dpm_mclk").read_text(encoding="utf-8").splitlines():
                if "*" in line:
                    m = re.search(r"(\d+)\s*Mhz", line, re.IGNORECASE)
                    if m:
                        stats["memory_clock_mhz"] = float(m.group(1))
                    break
        except OSError:
            pass

        return stats

    # --- Private helpers: disk -----------------------------------------

    @staticmethod
    def _disk_is_standby(path: str) -> bool:
        """Check if a disk is in standby/sleep without resetting its idle timer.

        Uses hdparm -C which sends ATA CHECK POWER MODE — this does NOT
        reset the drive's idle timer, unlike smartctl which sends
        IDENTIFY DEVICE and can prevent spin-down.
        """
        try:
            out = subprocess.run(
                ["hdparm", "-C", path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                stdout_upper = out.stdout.upper()
                if "STANDBY" in stdout_upper or "SLEEPING" in stdout_upper:
                    return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return False

    @staticmethod
    def _disk_temperature(path: str, dev: str, *, rotational: bool = False) -> float | None:
        """Read disk temp without affecting the drive's idle timer.

        Tries hwmon / drivetemp first (kernel-cached, no ATA commands).
        Falls back to smartctl only for non-rotational drives; rotational
        drives skip the smartctl fallback so their idle timer is not
        reset and they can spin down normally.
        """
        # Try hwmon / drivetemp (kernel module) — no spinup risk
        for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                name = (hwmon / "name").read_text(encoding="utf-8").strip()
                if name == "drivetemp":
                    device_link = hwmon / "device"
                    if device_link.is_symlink():
                        target = device_link.readlink().name
                        block_path = Path(f"/sys/block/{dev}/device")
                        if block_path.is_symlink():
                            block_target = block_path.readlink().name
                            if target == block_target:
                                temp_file = hwmon / "temp1_input"
                                if temp_file.exists():
                                    return round(int(temp_file.read_text(encoding="utf-8").strip()) / 1000.0, 1)
            except (OSError, ValueError):
                continue

        # NVMe — try device-specific hwmon first, then /sys/class/hwmon
        if dev.startswith("nvme"):
            for hwmon in sorted(Path(f"/sys/block/{dev}/device/hwmon").glob("hwmon*")):
                try:
                    temp_file = hwmon / "temp1_input"
                    if temp_file.exists():
                        return round(int(temp_file.read_text(encoding="utf-8").strip()) / 1000.0, 1)
                except (OSError, ValueError):
                    continue
            # Fallback: scan /sys/class/hwmon for NVMe controller hwmon.
            # hwmon "device" symlink (e.g. ../../nvme0) names the controller.
            ctrl_match = re.match(r"(nvme\d+)", dev)
            if ctrl_match:
                ctrl_name = ctrl_match.group(1)
                for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
                    try:
                        name = (hwmon / "name").read_text(encoding="utf-8").strip()
                        if name != "nvme":
                            continue
                        device_link = hwmon / "device"
                        if device_link.is_symlink() and os.readlink(device_link).split("/")[-1] == ctrl_name:
                            temp_file = hwmon / "temp1_input"
                            if temp_file.exists():
                                return round(int(temp_file.read_text(encoding="utf-8").strip()) / 1000.0, 1)
                    except (OSError, ValueError):
                        continue

        # Skip smartctl for rotational drives — ATA commands reset the
        # idle timer and would prevent the drive from spinning down.
        if rotational:
            LOG.debug(
                "No hwmon/drivetemp for %s — load the drivetemp kernel "
                "module for HDD temperature without preventing spin-down",
                dev,
            )
            return None

        # Fallback: smartctl (non-rotational drives only — no spin-down concern)
        try:
            out = subprocess.run(
                ["smartctl", "--nocheck", "standby", "-A", path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 2:
                return None  # standby — skip
            for line in out.stdout.splitlines():
                if "Temperature_Celsius" in line or "Airflow_Temperature" in line:
                    parts = line.split()
                    for part in reversed(parts):
                        try:
                            val = int(part)
                            if 0 < val < 120:
                                return float(val)
                        except ValueError:
                            continue
                if line.strip().startswith("Temperature:"):
                    m = re.search(r"(\d+)\s*Celsius", line)
                    if m:
                        return float(m.group(1))
        except FileNotFoundError:
            LOG.warning("smartctl not found in PATH — disk temperature unavailable")
        except subprocess.TimeoutExpired:
            LOG.debug("smartctl timed out reading temperature for %s", path)
        return None

    @staticmethod
    def _disk_health(path: str) -> str | None:
        """Get SMART overall health. Returns 'PASSED', 'FAILED', or None."""
        try:
            out = subprocess.run(
                ["smartctl", "--nocheck", "standby", "-H", path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 2:
                return None  # standby
            for line in out.stdout.splitlines():
                if "overall-health" in line.lower() or "smart health status" in line.lower():
                    if "PASSED" in line.upper() or "OK" in line.upper():
                        return "PASSED"
                    elif "FAILED" in line.upper():
                        return "FAILED"
                    return line.split(":")[-1].strip()
        except FileNotFoundError:
            LOG.warning("smartctl not found in PATH — disk health unavailable")
        except subprocess.TimeoutExpired:
            LOG.debug("smartctl timed out reading health for %s", path)
        return None

    # --- Private helpers: node -----------------------------------------

    def _node_cpu(self) -> float:
        """Compute CPU% from /proc/stat delta between polls."""
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:9]]
        total, idle = sum(vals), vals[3] + vals[4]

        if self._prev_node_cpu is None:
            self._prev_node_cpu = (total, idle)
            return 0.0

        pt, pi = self._prev_node_cpu
        self._prev_node_cpu = (total, idle)
        dt = total - pt
        return round((1 - (idle - pi) / dt) * 100, 1) if dt else 0.0

    @staticmethod
    def _node_mem_and_swap() -> dict:
        """Read memory and swap stats from a single /proc/meminfo pass."""
        needed = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
        fields: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, rest = line.split(":", 1)
                if key in needed:
                    fields[key] = int(rest.split()[0]) * 1024  # kB -> bytes
                    if len(fields) == len(needed):
                        break
        mem_total = fields.get("MemTotal", 0)
        mem_used = mem_total - fields.get("MemAvailable", 0)
        swap_total = fields.get("SwapTotal", 0)
        swap_used = swap_total - fields.get("SwapFree", 0)
        return {
            "mem_used_gb": round(mem_used * BYTES_TO_GB, 2),
            "mem_total_gb": round(mem_total * BYTES_TO_GB, 2),
            "mem_used_pct": round(mem_used / mem_total * 100, 1) if mem_total else 0,
            "swap_used_gb": round(swap_used * BYTES_TO_GB, 2),
            "swap_total_gb": round(swap_total * BYTES_TO_GB, 2),
            "swap_used_pct": round(swap_used / swap_total * 100, 1) if swap_total else 0,
        }

    @staticmethod
    def _node_uptime() -> float:
        """Return node uptime in hours."""
        try:
            secs = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
            return round(secs / 3600, 2)
        except (OSError, ValueError):
            return 0.0

    @staticmethod
    def _node_loadavg() -> dict:
        """Read 1/5/15 min load averages from /proc/loadavg as CPU %."""
        try:
            parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
            ncpu = LocalCollector._cpu_count()
            return {
                "load_1m": round(float(parts[0]) / ncpu * 100, 1),
                "load_5m": round(float(parts[1]) / ncpu * 100, 1),
                "load_15m": round(float(parts[2]) / ncpu * 100, 1),
            }
        except (OSError, ValueError, IndexError):
            return {"load_1m": 0.0, "load_5m": 0.0, "load_15m": 0.0}

    @staticmethod
    def _cpu_count() -> int:
        """Return the number of online CPUs from /proc/stat."""
        try:
            count = 0
            with open("/proc/stat", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("cpu") and line[3:4].isdigit():
                        count += 1
            return count or 1
        except OSError:
            return 1

    @staticmethod
    def _node_cpu_freq() -> float:
        """Read current average CPU frequency in MHz from /proc/cpuinfo."""
        try:
            freqs = []
            with open("/proc/cpuinfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("cpu MHz"):
                        freqs.append(float(line.split(":", 1)[1]))
            return round(sum(freqs) / len(freqs), 0) if freqs else 0.0
        except (OSError, ValueError):
            return 0.0

    # --- Private helpers: cgroups --------------------------------------

    def _find_cgroup(self, guest_type: str, vmid: int) -> Path | None:
        """Locate the cgroup directory for a running guest."""
        if not self._cgroup_v2:
            return self._find_cgroup_v1(guest_type, vmid)

        if guest_type == "qemu":
            # Try PID file → /proc/{pid}/cgroup first
            pid = self._qemu_pid(vmid)
            if pid is not None:
                cg = self._cgroup_from_pid(pid)
                if cg is not None:
                    return cg
                LOG.debug("QEMU %d: PID %d found but cgroup lookup from /proc failed", vmid, pid)
            else:
                LOG.debug("QEMU %d: no PID file in /run/qemu-server/ or /var/run/qemu-server/", vmid)

            # Fallback: try well-known systemd cgroup scope paths
            for pattern in (
                f"qemu.slice/{vmid}.scope",
                f"machine.slice/qemu-{vmid}.scope",
            ):
                cg = CGROUP_ROOT / pattern
                if cg.exists():
                    LOG.debug("QEMU %d: found cgroup via direct path %s", vmid, cg)
                    return cg

            # Scan the cgroup tree for any scope directory containing the VMID
            cg = self._scan_cgroup_tree(vmid)
            if cg is not None:
                LOG.info("QEMU %d: found cgroup via tree scan: %s", vmid, cg)
                return cg

            # Last resort: scan /proc for the QEMU process
            fallback_pid = self._qemu_pid_from_proc(vmid)
            if fallback_pid is not None:
                LOG.debug("QEMU %d: found PID %d via /proc scan", vmid, fallback_pid)
                cg = self._cgroup_from_pid(fallback_pid)
                if cg is not None:
                    return cg

            LOG.warning("QEMU %d: all cgroup lookup methods failed — reporting as stopped", vmid)
            return None

        # LXC — try known cgroup v2 paths
        for subdir in (f"lxc/{vmid}", f"lxc.payload.{vmid}"):
            cg = CGROUP_ROOT / subdir
            if cg.exists():
                return cg
        LOG.warning("LXC %d: cgroup not found under %s — reporting as stopped", vmid, CGROUP_ROOT)
        return None

    @staticmethod
    def _find_cgroup_v1(guest_type: str, vmid: int) -> Path | None:
        """Fallback for cgroup v1 (older Proxmox)."""
        if guest_type == "lxc":
            cg = Path(f"/sys/fs/cgroup/memory/lxc/{vmid}")
            if cg.exists():
                return cg
        elif guest_type == "qemu":
            pid = LocalCollector._qemu_pid(vmid)
            if pid is None:
                LOG.debug("QEMU %d (v1): no PID file found", vmid)
                return None
            try:
                cg_line = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
                for part in cg_line.strip().splitlines():
                    if ":memory:" in part:
                        rel = part.split(":")[-1].lstrip("/")
                        cg = Path(f"/sys/fs/cgroup/memory/{rel}")
                        if cg.exists():
                            return cg
            except (OSError, ValueError):
                pass
        return None

    @staticmethod
    def _qemu_pid(vmid: int) -> int | None:
        """Read QEMU process PID from /var/run/qemu-server/{vmid}.pid."""
        for base in ("/var/run/qemu-server", "/run/qemu-server"):
            try:
                return int(Path(f"{base}/{vmid}.pid").read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
        return None

    def _cgroup_from_pid(self, pid: int) -> Path | None:
        """Resolve a PID's cgroup v2 path, returning the directory or None."""
        try:
            raw = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        # cgroup v2 format: "0::/path/to/cgroup"
        for line in raw.splitlines():
            if "::" in line:
                rel = line.split("::", 1)[-1].lstrip("/")
                if rel:
                    cg = CGROUP_ROOT / rel
                    if cg.exists():
                        return cg
        return None

    @staticmethod
    def _qemu_pid_from_proc(vmid: int) -> int | None:
        """Fallback: find QEMU PID by scanning /proc/*/cmdline for -id {vmid}."""
        needle = f"-id\x00{vmid}\x00".encode()
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    cmdline = (entry / "cmdline").read_bytes()
                    if needle in cmdline and (b"kvm" in cmdline or b"qemu" in cmdline):
                        return int(entry.name)
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return None

    @staticmethod
    def _scan_cgroup_tree(vmid: int) -> Path | None:
        """Scan /sys/fs/cgroup for a scope directory matching the VMID.

        Checks common parent slices (qemu.slice, machine.slice, system.slice)
        for child directories whose names contain the VMID and have cpu.stat
        (proving they are real cgroup leaves).  This is safe for the
        no-spinup requirement since /sys/fs/cgroup is a virtual filesystem.
        """
        vmid_str = str(vmid)
        for parent in ("qemu.slice", "machine.slice", "system.slice", ""):
            base = CGROUP_ROOT / parent if parent else CGROUP_ROOT
            if not base.is_dir():
                continue
            try:
                for child in base.iterdir():
                    if not child.is_dir():
                        continue
                    if vmid_str not in child.name:
                        continue
                    # Verify it's a real cgroup (has cpu.stat or cgroup.procs)
                    if (child / "cpu.stat").exists() or (child / "cgroup.procs").exists():
                        return child
            except OSError:
                continue
        return None

    # --- /proc-based fallback for QEMU guests --------------------------

    def _find_qemu_pid(self, vmid: int) -> int | None:
        """Find QEMU PID via PID file or /proc scan."""
        pid = self._qemu_pid(vmid)
        if pid is not None and Path(f"/proc/{pid}").exists():
            return pid
        return self._qemu_pid_from_proc(vmid)

    def _proc_guest_status(self, guest_type: str, vmid: int, pid: int) -> dict:
        """Collect guest metrics from /proc/{pid}/stat and config file.

        Used as a fallback when cgroup is not accessible.  Reads CPU time
        from /proc/{pid}/stat and memory from /proc/{pid}/statm.  The
        configured memory limit is read from the PVE config file.
        """
        cpu = self._proc_cpu(guest_type, vmid, pid)
        mem = self._proc_memory(vmid, pid)
        return {
            "status": "running",
            "cpu": cpu,
            "mem_used_gb": mem["used_gb"],
            "mem_used_pct": mem["used_pct"],
        }

    def _proc_cpu(self, guest_type: str, vmid: int, pid: int) -> float:
        """Compute CPU% from /proc/{pid}/stat utime+stime delta."""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # Fields after the comm (which can contain spaces/parens)
            parts = stat.rsplit(")", 1)[-1].split()
            # utime is field 14, stime field 15 (0-indexed after ")": 11, 12)
            utime = int(parts[11])
            stime = int(parts[12])
        except (OSError, ValueError, IndexError):
            return 0.0

        clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        usage_usec = (utime + stime) * 1_000_000 // clk_tck

        now = time.monotonic_ns()
        key = (guest_type, vmid)
        prev = self._prev_guest_cpu.get(key)
        self._prev_guest_cpu[key] = (usage_usec, now)

        if prev is None:
            return 0.0
        prev_usage, prev_time = prev
        dt_us = (now - prev_time) / 1_000
        du = usage_usec - prev_usage
        if dt_us <= 0:
            return 0.0
        cores = self._guest_cores(guest_type, vmid)
        return round(du / dt_us / cores * 100, 1)

    def _pvesh_guest_status(self, guest_type: str, vmid: int) -> dict | None:
        """Query guest status via pvesh (Proxmox API).

        Uses ``pvesh get /nodes/{node}/{type}/{vmid}/status/current`` to
        read the same values the Proxmox UI shows, including balloon
        memory for VMs and accurate memory for CTs.

        Returns None when pvesh is unavailable, the guest is not found,
        or live stats are not yet populated (mem == 0 for a running guest).
        """
        api_type = guest_type if guest_type == "qemu" else "lxc"
        try:
            out = subprocess.run(
                [
                    "pvesh", "get",
                    f"/nodes/{self.node}/{api_type}/{vmid}/status/current",
                    "--output-format", "json",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode != 0:
                return None
            data = json.loads(out.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError,
                subprocess.SubprocessError):
            return None

        status = data.get("status", "unknown")
        if status != "running":
            return {"status": status, "cpu": 0.0, "mem_used_gb": 0.0, "mem_used_pct": 0.0}

        maxmem = data.get("maxmem", 0)
        mem = data.get("mem", 0)
        cpu_frac = data.get("cpu", 0.0)

        # If pvestatd hasn't populated live stats yet, mem will be 0
        # for a running guest — fall through to cgroup for live data.
        if not mem:
            return None

        return {
            "status": "running",
            "cpu": round(cpu_frac * 100, 1),
            "mem_used_gb": round(mem * BYTES_TO_GB, 2),
            "mem_used_pct": round(mem / maxmem * 100, 1) if maxmem else 0.0,
        }

    @staticmethod
    def _proc_memory(vmid: int, pid: int) -> dict:
        """Read memory from /proc/{pid}/statm and config-defined limit."""
        page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        try:
            parts = Path(f"/proc/{pid}/statm").read_text(encoding="utf-8").split()
            rss_bytes = int(parts[1]) * page_size
        except (OSError, ValueError, IndexError):
            rss_bytes = 0

        # Read configured memory limit from PVE config (in MB)
        maxmem_bytes = 0
        conf = LocalCollector._pve_conf_path("qemu", vmid)
        try:
            for line in conf.read_text(encoding="utf-8").splitlines():
                if line.startswith("memory:"):
                    maxmem_bytes = int(line.split(":")[1].strip()) * 1_048_576  # MB -> bytes
                    break
        except (OSError, ValueError):
            pass

        return {
            "used_gb": round(rss_bytes * BYTES_TO_GB, 2),
            "used_pct": round(rss_bytes / maxmem_bytes * 100, 1) if maxmem_bytes else 0.0,
        }

    # --- cgroup-based memory/CPU helpers -------------------------------

    def _cg_memory(self, cg: Path) -> dict:
        """Read memory usage from a cgroup directory.

        Subtracts all file-backed cache from the raw cgroup counter to
        match what Proxmox reports (reclaimable page-cache — both active
        and inactive — is not "used" memory).
        """
        if self._cgroup_v2:
            current = self._read_int(cg / "memory.current")
            max_raw = (cg / "memory.max").read_text(encoding="utf-8").strip() if (cg / "memory.max").exists() else "max"
            maxmem = 0 if max_raw == "max" else int(max_raw)
            cache = self._cg_mem_stat_field(cg / "memory.stat", "file")
        else:
            current = self._read_int(cg / "memory.usage_in_bytes")
            maxmem = self._read_int(cg / "memory.limit_in_bytes")
            cache = self._cg_mem_stat_field(cg / "memory.stat", "total_cache")
        used = max(current - cache, 0)
        return {
            "used_gb": round(used * BYTES_TO_GB, 2),
            "used_pct": round(used / maxmem * 100, 1) if maxmem else 0.0,
        }

    @staticmethod
    def _cg_mem_stat_field(stat_path: Path, field: str) -> int:
        """Read a single integer field from a cgroup memory.stat file."""
        try:
            for line in stat_path.read_text(encoding="utf-8").splitlines():
                if line.startswith(field):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
        except (OSError, ValueError):
            pass
        return 0

    def _cg_cpu(self, guest_type: str, vmid: int, cg: Path) -> float:
        """Compute CPU% from cgroup cpu accounting delta between polls."""
        if self._cgroup_v2:
            usage_usec = self._cg_cpu_usage_v2(cg)
        else:
            usage_usec = self._cg_cpu_usage_v1(cg)
        if usage_usec is None:
            return 0.0

        now = time.monotonic_ns()
        key = (guest_type, vmid)
        prev = self._prev_guest_cpu.get(key)
        self._prev_guest_cpu[key] = (usage_usec, now)

        if prev is None:
            return 0.0

        prev_usage, prev_time = prev
        dt_us = (now - prev_time) / 1_000  # ns -> us
        du = usage_usec - prev_usage
        if dt_us <= 0:
            return 0.0

        cores = self._guest_cores(guest_type, vmid)
        return round(du / dt_us / cores * 100, 1)

    @staticmethod
    def _cg_cpu_usage_v2(cg: Path) -> int | None:
        """Read CPU usage in microseconds from cgroup v2 cpu.stat."""
        try:
            for line in (cg / "cpu.stat").read_text(encoding="utf-8").splitlines():
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
        except (OSError, ValueError):
            pass
        return None

    @staticmethod
    def _cg_cpu_usage_v1(cg: Path) -> int | None:
        """Read CPU usage from cgroup v1 cpuacct.usage (nanoseconds -> microseconds)."""
        cpu_cg = cg.parent.parent / "cpu,cpuacct" / cg.relative_to(cg.parent.parent / "memory")
        try:
            return int(cpu_cg.joinpath("cpuacct.usage").read_text(encoding="utf-8").strip()) // 1_000
        except (OSError, ValueError):
            pass
        return None

    @staticmethod
    def _guest_cores(guest_type: str, vmid: int) -> int:
        """Read the configured core count for a guest from its PVE config."""
        conf = LocalCollector._pve_conf_path(guest_type, vmid)
        try:
            for line in conf.read_text(encoding="utf-8").splitlines():
                if line.startswith("cores:"):
                    return max(1, int(line.split(":")[1].strip()))
        except (OSError, ValueError):
            pass
        return 1

    @staticmethod
    def _pve_conf_path(guest_type: str, vmid: int) -> Path:
        """Return the path to a guest's PVE configuration file."""
        if guest_type == "qemu":
            return Path(f"/etc/pve/qemu-server/{vmid}.conf")
        return Path(f"/etc/pve/lxc/{vmid}.conf")

    @staticmethod
    def _read_int(path: Path) -> int:
        """Read a single integer from a file, returning 0 on failure."""
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    # --- Private helpers: guest filesystems -----------------------------

    @staticmethod
    def _cg_container_pid(cg: Path) -> int | None:
        """Find any PID from a container's cgroup tree.

        Walks sub-cgroups because systemd containers move all processes
        into child cgroups (init.scope, system.slice, etc.), leaving the
        top-level cgroup.procs empty.
        """
        try:
            for procs_file in cg.rglob("cgroup.procs"):
                try:
                    content = procs_file.read_text(encoding="utf-8").strip()
                    if content:
                        return int(content.splitlines()[0].strip())
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return None

    @staticmethod
    def _filesystems_from_mountinfo(pid: int) -> list[dict]:
        """Read mount points and usage from /proc/<pid>/mountinfo + statvfs."""
        filesystems: list[dict] = []
        root = f"/proc/{pid}/root"
        try:
            lines = Path(f"/proc/{pid}/mountinfo").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

        skip_fstypes = frozenset({
            "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2",
            "devpts", "mqueue", "hugetlbfs", "debugfs", "tracefs",
            "securityfs", "pstore", "bpf", "configfs", "fusectl",
            "nsfs", "squashfs", "autofs", "fuse.lxcfs",
        })

        seen_devs: set[str] = set()
        for line in lines:
            parts = line.split()
            if "-" not in parts:
                continue
            sep_idx = parts.index("-")
            if sep_idx + 2 >= len(parts):
                continue
            mount_point = parts[4].replace("\\040", " ").replace("\\011", "\t")
            fstype = parts[sep_idx + 1]
            device = parts[sep_idx + 2]

            if fstype in skip_fstypes:
                continue
            dev_key = f"{device}:{fstype}"
            if dev_key in seen_devs:
                continue
            seen_devs.add(dev_key)

            try:
                st = os.statvfs(f"{root}{mount_point}")
                total = st.f_frsize * st.f_blocks
                if total == 0:
                    continue
                avail = st.f_frsize * st.f_bavail
                used = total - (st.f_frsize * st.f_bfree)
                filesystems.append({
                    "mountpoint": mount_point,
                    "fstype": fstype,
                    "total_gb": round(total * BYTES_TO_GB, 2),
                    "used_gb": round(used * BYTES_TO_GB, 2),
                    "available_gb": round(avail * BYTES_TO_GB, 2),
                    "used_pct": round(used / total * 100, 1),
                })
            except OSError:
                continue
        return filesystems
