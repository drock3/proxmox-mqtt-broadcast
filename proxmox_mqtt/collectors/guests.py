"""Guest (QEMU / LXC) metric collection — discovery, status, filesystems."""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from ..config import BYTES_TO_GB, CGROUP_ROOT_PATH

LOG = logging.getLogger(__name__)

CGROUP_ROOT = Path(CGROUP_ROOT_PATH)


class GuestCollector:
    """Collects guest metrics from cgroups, procfs, and pvesh."""

    def __init__(self, node: str) -> None:
        self.node = node
        self._cgroup_v2 = (CGROUP_ROOT / "cgroup.controllers").exists()
        self._prev_cpu: dict[tuple, tuple] = {}

    # --- Discovery -----------------------------------------------------

    @staticmethod
    def discover() -> list[dict]:
        """Discover all QEMU VMs and LXC containers from /etc/pve."""
        guests: list[dict] = []
        cli_names = GuestCollector._guest_names_from_cli()
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

    # --- Status --------------------------------------------------------

    def status(self, guest_type: str, vmid: int) -> dict | None:
        """Return status dict for a guest, or None if not found."""
        # Prefer pvesh — returns the same values as the Proxmox UI
        pvesh = self._pvesh_status(guest_type, vmid)
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
                return self._proc_status(guest_type, vmid, pid)

        conf = self._pve_conf_path(guest_type, vmid)
        if conf.exists():
            return {"status": "stopped", "cpu": 0.0, "mem_used_gb": 0.0, "mem_used_pct": 0.0}
        return None

    # --- Filesystems ---------------------------------------------------

    def filesystems(self, guest_type: str, vmid: int) -> list[dict]:
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

    # --- pvesh ---------------------------------------------------------

    def _pvesh_status(self, guest_type: str, vmid: int) -> dict | None:
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

    # --- /proc-based fallback ------------------------------------------

    def _find_qemu_pid(self, vmid: int) -> int | None:
        """Find QEMU PID via PID file or /proc scan."""
        pid = self._qemu_pid(vmid)
        if pid is not None and Path(f"/proc/{pid}").exists():
            return pid
        return self._qemu_pid_from_proc(vmid)

    def _proc_status(self, guest_type: str, vmid: int, pid: int) -> dict:
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
        prev = self._prev_cpu.get(key)
        self._prev_cpu[key] = (usage_usec, now)

        if prev is None:
            return 0.0
        prev_usage, prev_time = prev
        dt_us = (now - prev_time) / 1_000
        du = usage_usec - prev_usage
        if dt_us <= 0:
            return 0.0
        cores = self._guest_cores(guest_type, vmid)
        return round(du / dt_us / cores * 100, 1)

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
        conf = GuestCollector._pve_conf_path("qemu", vmid)
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

    # --- Cgroup helpers ------------------------------------------------

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
            pid = GuestCollector._qemu_pid(vmid)
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
        prev = self._prev_cpu.get(key)
        self._prev_cpu[key] = (usage_usec, now)

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
        conf = GuestCollector._pve_conf_path(guest_type, vmid)
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

    # --- Filesystem helpers --------------------------------------------

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
            "nsfs", "squashfs", "autofs", "fuse.lxcfs", "efivarfs",
        })

        skip_mount_prefixes = ("/sys/", "/proc/", "/dev/")

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
            if any(mount_point.startswith(p) for p in skip_mount_prefixes):
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
