"""GPU discovery and statistics collection (Intel, AMD, NVIDIA)."""

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path

LOG = logging.getLogger(__name__)


class GpuCollector:
    """Discovers and collects stats for Intel, AMD, and NVIDIA GPUs."""

    @staticmethod
    def discover() -> list[dict]:
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
                name = GpuCollector._name_from_sysfs(card)
                # DRM card numbering shifts when other GPUs are added, so key
                # the GPU on its stable PCI address instead.
                pci_slot = GpuCollector._pci_slot_for_card(card)
                gpu_id = (
                    "pci_" + re.sub(r"[^a-z0-9]+", "_", pci_slot.lower())
                    if pci_slot
                    else card.name
                )
                gpus.append({
                    "id": gpu_id,
                    "vendor": vendor,
                    "driver": driver,
                    "name": name,
                    "sysfs_path": str(card),
                })

        gpus.extend(GpuCollector._discover_nvidia())
        return gpus

    def stats(self, gpus: list[dict]) -> dict[str, dict]:
        """Collect live stats for all discovered GPUs.

        Returns:
            Dict mapping GPU id to a stats dict with vendor-specific keys.
        """
        result: dict[str, dict] = {}

        nvidia_gpus = [g for g in gpus if g["vendor"] == "nvidia"]
        if nvidia_gpus:
            nvidia_data = self._nvidia_stats()
            nvidia_procs = self._nvidia_processes()
            for g in nvidia_gpus:
                idx = g["nvidia_index"]
                if idx in nvidia_data:
                    stats = nvidia_data[idx]
                    count, proc_mb = nvidia_procs.get(g.get("uuid", ""), (0, 0.0))
                    stats["processes"] = count
                    stats["process_memory_mb"] = proc_mb
                    result[g["id"]] = stats

        for g in gpus:
            if g["vendor"] == "intel":
                result[g["id"]] = self._intel_stats(Path(g["sysfs_path"]), g["driver"])
            elif g["vendor"] == "amd":
                result[g["id"]] = self._amd_stats(Path(g["sysfs_path"]))

        return result

    # --- Discovery helpers ---------------------------------------------

    @staticmethod
    def _name_from_sysfs(card: Path) -> str:
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
    def _discover_nvidia() -> list[dict]:
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

    # --- NVIDIA stats --------------------------------------------------

    # nvidia-smi --query-gpu field -> published key (None = parsed positionally)
    _NVIDIA_FIELDS: tuple[tuple[str, str | None], ...] = (
        ("index", None),
        ("pci.bus_id", None),
        ("utilization.gpu", "utilization_pct"),
        ("utilization.memory", "memory_bandwidth_pct"),
        ("utilization.encoder", "encoder_pct"),
        ("utilization.decoder", "decoder_pct"),
        ("memory.total", "memory_total_mb"),
        ("memory.used", "memory_used_mb"),
        ("memory.free", "memory_free_mb"),
        ("temperature.gpu", "temperature_c"),
        ("power.draw", "power_w"),
        ("enforced.power.limit", "power_limit_w"),
        ("fan.speed", "fan_speed_pct"),
        ("clocks.current.graphics", "clock_mhz"),
        ("clocks.max.graphics", "clock_max_mhz"),
        ("clocks.current.memory", "memory_clock_mhz"),
        ("pcie.link.gen.current", "pcie_gen"),
        ("pcie.link.width.current", "pcie_width"),
    )

    @staticmethod
    def _nvidia_stats() -> dict[int, dict]:
        """Query all NVIDIA GPU stats via a single nvidia-smi call."""
        query = ",".join(f for f, _ in GpuCollector._NVIDIA_FIELDS)
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    f"--query-gpu={query},pstate",
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

        expected = len(GpuCollector._NVIDIA_FIELDS) + 1  # + pstate
        result: dict[int, dict] = {}
        bus_ids: dict[int, str] = {}
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < expected:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            bus_ids[idx] = parts[1].upper()

            stats: dict = {}
            for pos, (_, key) in enumerate(GpuCollector._NVIDIA_FIELDS):
                if key:
                    stats[key] = _to_float(parts[pos])

            mem_total, mem_used = stats["memory_total_mb"], stats["memory_used_mb"]
            stats["memory_used_pct"] = (
                round(mem_used / mem_total * 100, 1)
                if mem_total and mem_used is not None
                else None
            )
            stats["performance_state"] = parts[len(GpuCollector._NVIDIA_FIELDS)]
            result[idx] = stats

        # power.draw is N/A on some cards — fall back to the driver's sample buffer
        if any(s["power_w"] is None for s in result.values()):
            samples = GpuCollector._nvidia_power_samples()
            for idx, stats in result.items():
                if stats["power_w"] is None:
                    stats["power_w"] = samples.get(bus_ids.get(idx, ""))

        for stats in result.values():
            power, limit = stats["power_w"], stats["power_limit_w"]
            stats["power_pct"] = (
                round(power / limit * 100, 1) if limit and power is not None else None
            )
        return result

    @staticmethod
    def _nvidia_power_samples() -> dict[str, float]:
        """Average power draw (W) per PCI bus id from nvidia-smi's power sample buffer."""
        try:
            out = subprocess.run(
                ["nvidia-smi", "-q", "-d", "POWER"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode != 0:
                return {}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {}

        result: dict[str, float] = {}
        bus_id: str | None = None
        in_samples = False
        for line in out.stdout.splitlines():
            stripped = line.strip()
            if re.fullmatch(r"GPU [0-9A-Fa-f:.]+", stripped):
                bus_id = stripped[4:].upper()
                in_samples = False
            elif stripped == "Power Samples":
                in_samples = True
            elif in_samples and stripped.startswith("Avg"):
                in_samples = False
                value = stripped.split(":", 1)[-1].split()
                if bus_id and value:
                    try:
                        result[bus_id] = float(value[0])
                    except ValueError:
                        pass
        return result

    @staticmethod
    def _nvidia_processes() -> dict[str, tuple[int, float]]:
        """Return per-GPU compute process count and total VRAM used, keyed by GPU UUID."""
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=gpu_uuid,used_gpu_memory",
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

        result: dict[str, tuple[int, float]] = {}
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            uuid = parts[0]
            try:
                used = float(parts[1])
            except ValueError:
                used = 0.0
            count, total = result.get(uuid, (0, 0.0))
            result[uuid] = (count + 1, round(total + used, 1))
        return result

    # --- Intel stats ---------------------------------------------------

    @staticmethod
    def _intel_stats(card: Path, driver: str) -> dict:
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
        pci_slot = GpuCollector._pci_slot_for_card(card)
        top_stats = GpuCollector._intel_gpu_top_stats(pci_slot)
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
        trimmed = raw.rstrip()
        trimmed = trimmed.replace("}\n{", "},\n{")
        if trimmed.endswith(","):
            trimmed = trimmed[:-1]
        if not trimmed.endswith("]"):
            trimmed += "]"

        data_list: list[dict] = []
        try:
            data_list = json.loads(trimmed)
        except json.JSONDecodeError:
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

    # --- AMD stats -----------------------------------------------------

    @staticmethod
    def _amd_stats(card: Path) -> dict:
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
