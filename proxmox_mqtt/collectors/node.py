"""Node-level metric collection — CPU, memory, swap, uptime, load, temperatures, fans."""

from pathlib import Path

from ..config import BYTES_TO_GB


class NodeCollector:
    """Collects host-level metrics from procfs and sysfs."""

    def __init__(self) -> None:
        self._prev_cpu: tuple[int, int] | None = None

    def status(self) -> dict:
        """Collect all node-level metrics (CPU, memory, swap, uptime, load, freq)."""
        mem_swap = self._mem_and_swap()
        return {
            "cpu": self._cpu(),
            **mem_swap,
            "uptime": self._uptime(),
            **self._loadavg(),
            "cpu_freq_mhz": self._cpu_freq(),
        }

    @staticmethod
    def temperatures() -> dict:
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

    @staticmethod
    def fan_speeds() -> dict:
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

    # --- Private helpers -----------------------------------------------

    def _cpu(self) -> float:
        """Compute CPU% from /proc/stat delta between polls."""
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:9]]
        total, idle = sum(vals), vals[3] + vals[4]

        if self._prev_cpu is None:
            self._prev_cpu = (total, idle)
            return 0.0

        pt, pi = self._prev_cpu
        self._prev_cpu = (total, idle)
        dt = total - pt
        return round((1 - (idle - pi) / dt) * 100, 1) if dt else 0.0

    @staticmethod
    def _mem_and_swap() -> dict:
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
    def _uptime() -> float:
        """Return node uptime in hours."""
        try:
            secs = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
            return round(secs / 3600, 2)
        except (OSError, ValueError):
            return 0.0

    @staticmethod
    def _loadavg() -> dict:
        """Read 1/5/15 min load averages from /proc/loadavg as CPU %."""
        try:
            parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
            ncpu = NodeCollector._cpu_count()
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
    def _cpu_freq() -> float:
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
