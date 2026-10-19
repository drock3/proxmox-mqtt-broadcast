"""Disk discovery, SMART monitoring, and I/O throughput collection."""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from ..config import BYTES_TO_MB

LOG = logging.getLogger(__name__)


class DiskCollector:
    """Discovers and monitors physical disks and their I/O throughput."""

    _drivetemp_warned: set[str] = set()

    def __init__(self) -> None:
        self._prev_io: dict[str, tuple] = {}
        self._cached_health: dict[str, str | None] = {}
        self._cached_temp: dict[str, float | None] = {}

    def discover(self) -> list[dict]:
        """Discover physical disks with model, serial, type, temp.

        Reads temperature and SMART health at startup (drives are
        expected to be spinning after boot).  Results are cached so
        that subsequent polling never touches rotational drives.
        """
        disks: list[dict] = []
        try:
            lsblk = subprocess.run(
                ["lsblk", "-dnpJbo", "NAME,TYPE,ROTA,MODEL,SERIAL,SIZE"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if lsblk.returncode != 0:
                return disks
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return disks

        try:
            devices = json.loads(lsblk.stdout).get("blockdevices", [])
        except (json.JSONDecodeError, KeyError):
            return disks

        for entry in devices:
            name = entry.get("name", "")
            dtype = entry.get("type", "")
            if dtype != "disk":
                continue
            dev = Path(name).name
            rota = entry.get("rota", False)
            model = entry.get("model") or "Unknown"
            serial = entry.get("serial") or "Unknown"
            raw_size = entry.get("size")
            size = round(int(raw_size) / 1_000_000_000, 1) if raw_size else None
            is_rotational = bool(rota)

            temp = None
            health = None
            standby = False

            if is_rotational:
                standby = DiskCollector._is_standby(name)

            if not standby:
                temp = DiskCollector._temperature(name, dev, rotational=is_rotational)
                if temp is not None:
                    health = DiskCollector._health(name)
                else:
                    temp, health = DiskCollector._smartctl_attrs(name)
                if is_rotational:
                    self._cached_temp[dev] = temp
                    self._cached_health[dev] = health
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

    def refresh_status(self, disk: dict) -> dict:
        """Refresh volatile fields for a known disk.

        For rotational drives, only hdparm -C (standby check) touches
        the drive.  Temperature and health are returned from the cache
        populated at startup.  For non-rotational drives, hwmon/smartctl
        are queried normally (no spin-down concern).
        """
        path = disk["path"]
        dev = disk["dev"]
        is_rot = disk["rotational"]

        if not is_rot:
            temp = DiskCollector._temperature(path, dev, rotational=False)
            if temp is not None:
                health = DiskCollector._health(path)
            else:
                temp, health = DiskCollector._smartctl_attrs(path)
            return {"temperature": temp, "health": health, "standby": False}

        standby = DiskCollector._is_standby(path)
        # Reading drivetemp hwmon or SMART issues an ATA command that resets the
        # drive's standby timer, so rotational drives are only ever probed once.
        if not standby and dev not in self._cached_temp:
            self._cached_temp[dev] = DiskCollector._temperature(path, dev, rotational=True)
            self._cached_health[dev] = DiskCollector._health(path)

        return {
            "temperature": None if standby else self._cached_temp.get(dev),
            "health": self._cached_health.get(dev),
            "standby": standby,
        }

    def io(self) -> dict:
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
            prev = self._prev_io.get(dev)
            self._prev_io[dev] = (read_bytes, write_bytes, now)
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

    # --- Private helpers -----------------------------------------------

    @staticmethod
    def _is_standby(path: str) -> bool:
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
    def _temperature(path: str, dev: str, *, rotational: bool = False) -> float | None:
        """Read disk temp from hwmon only (no ATA commands).

        Tries hwmon / drivetemp first (kernel-cached, no ATA commands)
        and NVMe device hwmon.  Returns None if hwmon cannot provide the
        temperature — callers should fall back to _smartctl_attrs().
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

        if rotational and dev not in DiskCollector._drivetemp_warned:
            DiskCollector._drivetemp_warned.add(dev)
            LOG.warning(
                "No hwmon/drivetemp for %s — load the drivetemp kernel "
                "module (`modprobe drivetemp`) for HDD temperature without "
                "resetting the drive idle timer",
                dev,
            )

        return None

    @staticmethod
    def _smartctl_attrs(path: str) -> tuple[float | None, str | None]:
        """Query SMART attributes and health in a single smartctl call.

        Returns (temperature, health).  Uses --nocheck standby so a
        sleeping drive is not woken.
        """
        temp: float | None = None
        health: str | None = None
        try:
            out = subprocess.run(
                ["smartctl", "--nocheck", "standby", "-A", "-H", path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode & 2:
                return None, None  # standby / device-open failure
            for line in out.stdout.splitlines():
                if temp is None:
                    if "Temperature_Celsius" in line or "Airflow_Temperature" in line:
                        parts = line.split()
                        for part in reversed(parts):
                            try:
                                val = int(part)
                                if 0 < val < 120:
                                    temp = float(val)
                                    break
                            except ValueError:
                                continue
                    elif line.strip().startswith("Temperature:"):
                        m = re.search(r"(\d+)\s*Celsius", line)
                        if m:
                            temp = float(m.group(1))
                if health is None:
                    low = line.lower()
                    if "overall-health" in low or "smart health status" in low:
                        if "PASSED" in line.upper() or "OK" in line.upper():
                            health = "PASSED"
                        elif "FAILED" in line.upper():
                            health = "FAILED"
                        else:
                            health = line.split(":")[-1].strip()
        except FileNotFoundError:
            LOG.warning("smartctl not found in PATH — disk SMART data unavailable")
        except subprocess.TimeoutExpired:
            LOG.debug("smartctl timed out reading SMART data for %s", path)
        return temp, health

    @staticmethod
    def _health(path: str) -> str | None:
        """Get SMART overall health. Returns 'PASSED', 'FAILED', or None."""
        try:
            out = subprocess.run(
                ["smartctl", "--nocheck", "standby", "-H", path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode & 2:
                return None  # standby / device-open failure
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
