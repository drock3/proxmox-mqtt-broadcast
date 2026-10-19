"""MQTT bridge with Home Assistant MQTT Discovery."""

import json
import logging
import re
import time

import paho.mqtt.client as mqtt

from .collectors import LocalCollector
from .config import KIB_TO_GB

LOG = logging.getLogger(__name__)


class MQTTBridge:
    """Publishes Proxmox metrics to an MQTT broker with HA Discovery."""

    def __init__(self, cfg: dict, collector: LocalCollector) -> None:
        self.cfg = cfg
        self.collector = collector
        self.prefix = cfg["mqtt"].get("topic_prefix", "proxmox")
        self.disc_prefix = cfg["mqtt"].get("discovery_prefix", "homeassistant")
        self.avail_topic = f"{self.prefix}/availability"
        self.node_name = cfg["node"]

        # Feature flags — all default to True (must be set before auto-discovery)
        enabled = cfg.get("enabled", {})
        self.en_guests = enabled.get("guests", True)
        self.en_zfs = enabled.get("zfs", True)
        self.en_storage = enabled.get("storage", cfg.get("storage", True))
        self.en_temperatures = enabled.get("temperatures", True)
        self.en_fans = enabled.get("fans", True)
        self.en_network = enabled.get("network", True)
        self.en_diskio = enabled.get("disk_io", True)
        self.en_disks = enabled.get("disks", True)
        self.en_gpus = enabled.get("gpus", True)
        self.en_updates = enabled.get("updates", True)

        # Resolve guests — auto-discover if omitted
        if self.en_guests:
            if "guests" in cfg:
                self.guests = cfg["guests"]
            else:
                self.guests = LocalCollector.discover_guests()
                LOG.info(
                    "Auto-discovered %d guest(s): %s",
                    len(self.guests),
                    ", ".join(f"{g['name']}({g['vmid']})" for g in self.guests),
                )
        else:
            self.guests = []

        # Resolve ZFS pools
        if self.en_zfs:
            if "zfs_pools" in cfg:
                self.zfs_pools = cfg["zfs_pools"]
            else:
                self.zfs_pools = LocalCollector.discover_zfs_pools()
                LOG.info(
                    "Auto-discovered %d ZFS pool(s): %s",
                    len(self.zfs_pools),
                    ", ".join(self.zfs_pools),
                )
        else:
            self.zfs_pools = []

        self._published_topics: set[str] = set()
        self._pending_topics: set[str] = set()
        self._rediscover_topic = f"{self.prefix}/{self.node_name}/rediscover"
        self._cached_storage: list[dict] = []

        # Cache pvesm at startup (queries ZFS pools which wakes drives)
        if self.en_storage:
            self._cached_storage = LocalCollector.storage_status()

        # Discover disks and network interfaces at startup
        if self.en_disks or self.en_diskio:
            self.disks = self.collector.discover_disks()
            LOG.info(
                "Discovered %d disk(s): %s",
                len(self.disks),
                ", ".join(d["dev"] for d in self.disks),
            )
        else:
            self.disks = []

        if self.en_network:
            self._net_ifaces = LocalCollector.discover_interfaces()
            LOG.info(
                "Discovered %d network interface(s): %s",
                len(self._net_ifaces),
                ", ".join(self._net_ifaces),
            )
        else:
            self._net_ifaces = []

        if self.en_gpus:
            self.gpus = LocalCollector.discover_gpus()
            LOG.info(
                "Discovered %d GPU(s): %s",
                len(self.gpus),
                ", ".join(f"{g['name']} ({g['vendor']})" for g in self.gpus),
            )
        else:
            self.gpus = []

        # MQTT client setup
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg["mqtt"].get("client_id", "proxmox-broadcast"),
        )
        if cfg["mqtt"].get("username"):
            self.client.username_pw_set(
                cfg["mqtt"]["username"],
                cfg["mqtt"].get("password", ""),
            )
        tls_cfg = cfg["mqtt"].get("tls", {})
        if tls_cfg.get("enabled"):
            import ssl

            self.client.tls_set(
                ca_certs=tls_cfg.get("ca_certs"),
                certfile=tls_cfg.get("certfile"),
                keyfile=tls_cfg.get("keyfile"),
                cert_reqs=ssl.CERT_NONE if tls_cfg.get("insecure") else ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            if tls_cfg.get("insecure"):
                self.client.tls_insecure_set(True)
            LOG.info("MQTT TLS enabled")
        self.client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self._auth_failed = False

    # -- MQTT callbacks -------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        connect_flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            LOG.error("MQTT connection failed: %s", reason_code)
            self._auth_failed = True
            self.client.disconnect()
            return
        LOG.info("MQTT connected")
        self._publish_discovery()
        self.client.publish(self.avail_topic, "online", qos=1, retain=True)
        self.client.subscribe(self._rediscover_topic, qos=1)

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: object,
        msg: mqtt.MQTTMessage,
    ) -> None:
        if msg.topic == self._rediscover_topic:
            LOG.info("Rediscover requested via MQTT")
            self._rediscover()

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure and not self._auth_failed:
            LOG.warning("MQTT disconnected: %s (will auto-reconnect)", reason_code)

    # -- Lifecycle ------------------------------------------------------

    def connect(self) -> None:
        """Connect to the MQTT broker and start the network loop."""
        self.client.connect(
            self.cfg["mqtt"]["host"],
            self.cfg["mqtt"].get("port", 1883),
            keepalive=60,
        )
        self.client.loop_start()

    def disconnect(self) -> None:
        """Publish offline status, stop the loop, and disconnect."""
        self.client.publish(self.avail_topic, "offline", qos=1, retain=True)
        time.sleep(0.5)
        self.client.loop_stop()
        self.client.disconnect()

    # -- HA MQTT Discovery ----------------------------------------------

    def _device(self, suffix: str, name: str, model: str) -> dict:
        """Build an HA device descriptor."""
        return {
            "identifiers": [f"proxmox_{self.node_name}_{suffix}"],
            "name": name,
            "manufacturer": "Proxmox",
            "model": model,
        }

    @staticmethod
    def _safe_mount(mount: str) -> str:
        """Convert a mount point path to a safe topic/UID component."""
        if mount == "/":
            return "root"
        return re.sub(r"[^a-zA-Z0-9]+", "_", mount).strip("_").lower() or "root"

    def _sensor(
        self,
        uid: str,
        name: str,
        state_topic: str,
        value_template: str,
        device: dict,
        *,
        unit: str | None = None,
        icon: str | None = None,
        state_class: str | None = "measurement",
    ) -> None:
        """Publish a single HA MQTT sensor discovery config."""
        payload: dict = {
            "name": name,
            "unique_id": uid,
            "state_topic": state_topic,
            "value_template": value_template,
            "availability_topic": self.avail_topic,
            "device": device,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if icon:
            payload["icon"] = icon
        if state_class:
            payload["state_class"] = state_class
        topic = f"{self.disc_prefix}/sensor/{uid}/config"
        self._pending_topics.add(topic)
        self.client.publish(topic, json.dumps(payload), qos=1, retain=True)

    def _publish_discovery(self) -> None:
        """Publish HA MQTT Discovery payloads for all enabled metric categories.

        Tracks every discovery topic published this round and removes
        stale entities (empty retained payload) that were in the
        previous round but not this one.
        """
        self._pending_topics = set()
        n = self.node_name

        # --- Node ---
        st = f"{self.prefix}/{n}/state"
        dev = self._device("node", f"Proxmox {n}", "Node")
        self._sensor(f"pve_{n}_cpu",        "CPU Usage",      st, "{{ value_json.cpu }}",          dev, unit="%",  icon="mdi:cpu-64-bit")
        self._sensor(f"pve_{n}_mem_used",    "Memory Used",    st, "{{ value_json.mem_used_gb }}",  dev, unit="GB", icon="mdi:memory")
        self._sensor(f"pve_{n}_mem_total",   "Memory Total",   st, "{{ value_json.mem_total_gb }}", dev, unit="GB", icon="mdi:memory")
        self._sensor(f"pve_{n}_mem_pct",     "Memory Usage",   st, "{{ value_json.mem_used_pct }}", dev, unit="%",  icon="mdi:memory")
        self._sensor(f"pve_{n}_swap_used",   "Swap Used",      st, "{{ value_json.swap_used_gb }}",  dev, unit="GB", icon="mdi:memory")
        self._sensor(f"pve_{n}_swap_total",  "Swap Total",     st, "{{ value_json.swap_total_gb }}", dev, unit="GB", icon="mdi:memory")
        self._sensor(f"pve_{n}_swap_pct",    "Swap Usage",     st, "{{ value_json.swap_used_pct }}", dev, unit="%",  icon="mdi:memory")
        self._sensor(f"pve_{n}_uptime",      "Uptime",         st, "{{ value_json.uptime }}",        dev, unit="h",  icon="mdi:clock-outline")
        self._sensor(f"pve_{n}_load_1m",     "Load 1m",        st, "{{ value_json.load_1m }}",       dev, unit="%", icon="mdi:gauge")
        self._sensor(f"pve_{n}_load_5m",     "Load 5m",        st, "{{ value_json.load_5m }}",       dev, unit="%", icon="mdi:gauge")
        self._sensor(f"pve_{n}_load_15m",    "Load 15m",       st, "{{ value_json.load_15m }}",      dev, unit="%", icon="mdi:gauge")
        self._sensor(f"pve_{n}_cpu_freq",    "CPU Frequency",  st, "{{ value_json.cpu_freq_mhz }}", dev, unit="MHz", icon="mdi:chip")

        # --- Node temperatures ---
        if self.en_temperatures:
            temp_st = f"{self.prefix}/{n}/temperatures/state"
            temps = self.collector.node_temperatures()
            for tname in temps:
                safe = re.sub(r"[^a-zA-Z0-9_]", "_", tname).lower()
                self._sensor(
                    f"pve_{n}_temp_{safe}", f"Temp {tname}", temp_st,
                    "{{ value_json." + tname + " }}", dev, unit="°C", icon="mdi:thermometer",
                )

        # --- Fan speeds ---
        if self.en_fans:
            fan_st = f"{self.prefix}/{n}/fans/state"
            fans = self.collector.fan_speeds()
            for fname in fans:
                safe = re.sub(r"[^a-zA-Z0-9_]", "_", fname).lower()
                self._sensor(
                    f"pve_{n}_fan_{safe}", f"Fan {fname}", fan_st,
                    "{{ value_json." + fname + " }}", dev, unit="RPM", icon="mdi:fan",
                )

        # --- Network interfaces ---
        if self.en_network:
            for iface in self._net_ifaces:
                n_st = f"{self.prefix}/{n}/net/{iface}/state"
                n_dev = self._device(f"net_{iface}", f"Net {iface}", "Network Interface")
                uid = f"pve_{n}_net_{iface}"
                self._sensor(f"{uid}_rx", "RX", n_st, "{{ value_json.rx_mb_sec }}", n_dev, unit="MB/s", icon="mdi:download-network")
                self._sensor(f"{uid}_tx", "TX", n_st, "{{ value_json.tx_mb_sec }}", n_dev, unit="MB/s", icon="mdi:upload-network")

        # --- Disks (I/O + SMART / info) ---
        if self.en_diskio or self.en_disks:
            for d in self.disks:
                ddev = d["dev"]
                d_dev = self._device(
                    f"disk_{ddev}", f"Disk {d['model']} ({ddev})",
                    f"{'HDD' if d['rotational'] else 'SSD/NVMe'}",
                )
                if self.en_diskio:
                    dio_st = f"{self.prefix}/{n}/diskio/{ddev}/state"
                    uid = f"pve_{n}_diskio_{ddev}"
                    self._sensor(f"{uid}_read",  "Read",  dio_st, "{{ value_json.read_mb_sec }}",  d_dev, unit="MB/s", icon="mdi:harddisk")
                    self._sensor(f"{uid}_write", "Write", dio_st, "{{ value_json.write_mb_sec }}", d_dev, unit="MB/s", icon="mdi:harddisk")
                if self.en_disks:
                    d_st = f"{self.prefix}/{n}/disk/{ddev}/state"
                    uid = f"pve_{n}_disk_{ddev}"
                    self._sensor(f"{uid}_temp",    "Temperature", d_st, "{{ value_json.temperature }}", d_dev, unit="°C", icon="mdi:thermometer")
                    self._sensor(f"{uid}_health",  "Health",      d_st, "{{ value_json.health }}",      d_dev, icon="mdi:heart-pulse", state_class=None)
                    self._sensor(f"{uid}_standby", "Standby",     d_st, "{{ value_json.standby }}",     d_dev, icon="mdi:sleep", state_class=None)
                    self._sensor(f"{uid}_model",   "Model",       d_st, "{{ value_json.model }}",       d_dev, icon="mdi:harddisk", state_class=None)
                    self._sensor(f"{uid}_serial",  "Serial",      d_st, "{{ value_json.serial }}",      d_dev, icon="mdi:identifier", state_class=None)
                    self._sensor(f"{uid}_size",    "Size",        d_st, "{{ value_json.size }}",        d_dev, unit="GB", icon="mdi:harddisk", state_class=None)
                    self._sensor(f"{uid}_path",    "Path",        d_st, "{{ value_json.dev_path }}",    d_dev, icon="mdi:folder-marker", state_class=None)

        # --- Guests (QEMU / LXC) ---
        if self.en_guests:
            for g in self.guests:
                vmid, gtype, gname = g["vmid"], g["type"], g["name"]
                g_st = f"{self.prefix}/{n}/{gtype}/{vmid}/state"
                g_dev = self._device(f"{gtype}_{vmid}", gname, gtype.upper())
                uid = f"pve_{n}_{gtype}_{vmid}"
                self._sensor(f"{uid}_status",   "Status",       g_st, "{{ value_json.status }}",       g_dev, icon="mdi:server", state_class=None)
                self._sensor(f"{uid}_cpu",       "CPU Usage",    g_st, "{{ value_json.cpu }}",          g_dev, unit="%",  icon="mdi:cpu-64-bit")
                self._sensor(f"{uid}_mem_used",  "Memory Used",  g_st, "{{ value_json.mem_used_gb }}",  g_dev, unit="GB", icon="mdi:memory")
                self._sensor(f"{uid}_mem_pct",   "Memory Usage", g_st, "{{ value_json.mem_used_pct }}", g_dev, unit="%",  icon="mdi:memory")

                # Filesystem mount points
                for fs in self.collector.guest_filesystems(gtype, vmid):
                    safe = self._safe_mount(fs["mountpoint"])
                    fs_st = f"{self.prefix}/{n}/{gtype}/{vmid}/fs/{safe}/state"
                    fs_uid = f"{uid}_fs_{safe}"
                    self._sensor(f"{fs_uid}_used_pct", f"Disk Usage {fs['mountpoint']}", fs_st, "{{ value_json.used_pct }}", g_dev, unit="%", icon="mdi:harddisk")
                    self._sensor(f"{fs_uid}_total",    f"Disk Total {fs['mountpoint']}", fs_st, "{{ value_json.total_gb }}", g_dev, unit="GB", icon="mdi:harddisk")
                    self._sensor(f"{fs_uid}_used",     f"Disk Used {fs['mountpoint']}",  fs_st, "{{ value_json.used_gb }}",  g_dev, unit="GB", icon="mdi:harddisk")
                    self._sensor(f"{fs_uid}_avail",    f"Disk Free {fs['mountpoint']}",  fs_st, "{{ value_json.available_gb }}", g_dev, unit="GB", icon="mdi:harddisk")

        # --- ZFS pools ---
        if self.en_zfs:
            for pool in self.zfs_pools:
                z_st = f"{self.prefix}/{n}/zfs/{pool}/state"
                z_dev = self._device(f"zfs_{pool}", f"ZFS {pool}", "ZFS Pool")
                self._sensor(f"pve_{n}_zfs_{pool}_used",   "Used %",    z_st, "{{ value_json.used_pct }}", z_dev, unit="%", icon="mdi:harddisk")
                self._sensor(f"pve_{n}_zfs_{pool}_total",  "Total",     z_st, "{{ value_json.total_gb }}", z_dev, unit="GB", icon="mdi:harddisk")
                self._sensor(f"pve_{n}_zfs_{pool}_used_gb", "Used",     z_st, "{{ value_json.used_gb }}",  z_dev, unit="GB", icon="mdi:harddisk")
                self._sensor(f"pve_{n}_zfs_{pool}_avail",  "Available", z_st, "{{ value_json.avail_gb }}", z_dev, unit="GB", icon="mdi:harddisk")
                self._sensor(f"pve_{n}_zfs_{pool}_health", "Health",    z_st, "{{ value_json.health }}",   z_dev, icon="mdi:heart-pulse", state_class=None)

        # --- Storage (all types via pvesm) ---
        if self.en_storage:
            storages = self._cached_storage
            if self.en_zfs:
                storages = [s for s in storages if s["type"] != "zfspool"]
            for s in storages:
                sname = s["name"]
                s_st = f"{self.prefix}/{n}/storage/{sname}/state"
                s_dev = self._device(f"storage_{sname}", f"Storage {sname}", s["type"])
                uid = f"pve_{n}_storage_{sname}"
                self._sensor(f"{uid}_status", "Status",    s_st, "{{ value_json.status }}",   s_dev, icon="mdi:harddisk", state_class=None)
                self._sensor(f"{uid}_total",  "Total",     s_st, "{{ value_json.total_gb }}", s_dev, unit="GB", icon="mdi:harddisk")
                self._sensor(f"{uid}_used",   "Used",      s_st, "{{ value_json.used_gb }}",  s_dev, unit="GB", icon="mdi:harddisk")
                self._sensor(f"{uid}_avail",  "Available", s_st, "{{ value_json.avail_gb }}", s_dev, unit="GB", icon="mdi:harddisk")
                self._sensor(f"{uid}_pct",    "Used %",    s_st, "{{ value_json.used_pct }}", s_dev, unit="%",  icon="mdi:harddisk")

        # --- GPUs (Intel, AMD, NVIDIA) ---
        if self.en_gpus:
            gpu_sensors: dict[str, list[tuple[str, str, str | None, str]]] = {
                "nvidia": [
                    ("utilization_pct",      "Utilization",        "%",   "mdi:expansion-card"),
                    ("memory_bandwidth_pct", "Memory Bandwidth",   "%",   "mdi:memory"),
                    ("encoder_pct",          "Encoder Usage",      "%",   "mdi:video-box"),
                    ("decoder_pct",          "Decoder Usage",      "%",   "mdi:video-box"),
                    ("memory_total_mb",      "VRAM Total",         "MB",  "mdi:memory"),
                    ("memory_used_mb",       "VRAM Used",          "MB",  "mdi:memory"),
                    ("memory_free_mb",       "VRAM Free",          "MB",  "mdi:memory"),
                    ("memory_used_pct",      "VRAM Usage",         "%",   "mdi:memory"),
                    ("temperature_c",        "Temperature",        "°C",  "mdi:thermometer"),
                    ("power_w",              "Power Draw",         "W",   "mdi:flash"),
                    ("power_limit_w",        "Power Limit",        "W",   "mdi:flash-outline"),
                    ("power_pct",            "Power Usage",        "%",   "mdi:flash"),
                    ("fan_speed_pct",        "Fan Speed",          "%",   "mdi:fan"),
                    ("clock_mhz",            "GPU Clock",          "MHz", "mdi:chip"),
                    ("clock_max_mhz",        "GPU Clock Max",      "MHz", "mdi:chip"),
                    ("memory_clock_mhz",     "Memory Clock",       "MHz", "mdi:chip"),
                    ("pcie_gen",             "PCIe Generation",    None,  "mdi:expansion-card-variant"),
                    ("pcie_width",           "PCIe Link Width",    None,  "mdi:expansion-card-variant"),
                    ("processes",            "Compute Processes",  None,  "mdi:application-cog"),
                    ("process_memory_mb",    "Process VRAM Used",  "MB",  "mdi:memory"),
                ],
                "amd": [
                    ("utilization_pct",  "Utilization",  "%",   "mdi:expansion-card"),
                    ("memory_total_mb",  "VRAM Total",   "MB",  "mdi:memory"),
                    ("memory_used_mb",   "VRAM Used",    "MB",  "mdi:memory"),
                    ("memory_used_pct",  "VRAM Usage",   "%",   "mdi:memory"),
                    ("temperature_c",    "Temperature",  "°C",  "mdi:thermometer"),
                    ("power_w",          "Power Draw",   "W",   "mdi:flash"),
                    ("clock_mhz",        "GPU Clock",    "MHz", "mdi:chip"),
                    ("memory_clock_mhz", "Memory Clock", "MHz", "mdi:chip"),
                ],
                "intel": [
                    ("utilization_pct",      "Utilization",       "%",     "mdi:expansion-card"),
                    ("rc6_pct",              "RC6 Residency",     "%",     "mdi:sleep"),
                    ("power_w",              "GPU Power",         "W",     "mdi:flash"),
                    ("power_package_w",      "Package Power",     "W",     "mdi:flash"),
                    ("clock_mhz",            "GPU Clock",         "MHz",   "mdi:chip"),
                    ("clock_max_mhz",        "GPU Clock Max",     "MHz",   "mdi:chip"),
                    ("clock_min_mhz",        "GPU Clock Min",     "MHz",   "mdi:chip"),
                    ("engine_render_3d_pct", "Render/3D Engine",  "%",     "mdi:rotate-3d"),
                    ("engine_blitter_pct",   "Blitter Engine",    "%",     "mdi:brush"),
                    ("engine_video_pct",     "Video Engine",      "%",     "mdi:video"),
                    ("engine_videoenhance_pct", "Video Enhance Engine", "%", "mdi:video-plus"),
                    ("imc_reads_mib_s",      "IMC Reads",         "MiB/s", "mdi:memory"),
                    ("imc_writes_mib_s",     "IMC Writes",        "MiB/s", "mdi:memory"),
                ],
            }
            for g in self.gpus:
                gpu_id = g["id"]
                safe_id = re.sub(r"[^a-zA-Z0-9_]", "_", gpu_id).lower()
                gpu_st = f"{self.prefix}/{n}/gpu/{gpu_id}/state"
                gpu_dev = self._device(f"gpu_{safe_id}", f"GPU {g['name']}", g["vendor"].upper())
                uid_prefix = f"pve_{n}_gpu_{safe_id}"
                for field, label, unit, icon in gpu_sensors.get(g["vendor"], []):
                    self._sensor(
                        f"{uid_prefix}_{field}", label, gpu_st,
                        "{{ value_json." + field + " }}", gpu_dev,
                        unit=unit, icon=icon,
                    )
                if g["vendor"] == "nvidia":
                    self._sensor(
                        f"{uid_prefix}_performance_state", "Performance State", gpu_st,
                        "{{ value_json.performance_state }}", gpu_dev,
                        icon="mdi:speedometer", state_class=None,
                    )

        # --- Pending updates ---
        if self.en_updates:
            up_st = f"{self.prefix}/{n}/updates/state"
            dev = self._device("node", f"Proxmox {n}", "Node")
            self._sensor(f"pve_{n}_updates_available",  "Updates Available",  up_st, "{{ value_json.updates_available }}",  dev, unit="updates", icon="mdi:package-up")
            self._sensor(f"pve_{n}_security_updates",   "Security Updates",   up_st, "{{ value_json.security_updates }}",   dev, unit="updates", icon="mdi:shield-alert")
            self._sensor(f"pve_{n}_reboot_required",    "Reboot Required",    up_st, "{{ value_json.reboot_required }}",    dev, icon="mdi:restart", state_class=None)

        # --- Rediscover button ---
        dev = self._device("node", f"Proxmox {n}", "Node")
        button_topic = f"{self.disc_prefix}/button/pve_{n}_rediscover/config"
        self._pending_topics.add(button_topic)
        self.client.publish(
            button_topic,
            json.dumps({
                "name": "Rediscover",
                "unique_id": f"pve_{n}_rediscover",
                "command_topic": self._rediscover_topic,
                "availability_topic": self.avail_topic,
                "device": dev,
                "icon": "mdi:refresh",
                "entity_category": "diagnostic",
            }),
            qos=1,
            retain=True,
        )

        # Remove stale discovery topics from the broker
        stale = self._published_topics - self._pending_topics
        if stale:
            LOG.info("Removing %d stale discovery topic(s)", len(stale))
            for topic in sorted(stale):
                self.client.publish(topic, "", qos=1, retain=True)
                LOG.debug("Removed stale topic: %s", topic)
        self._published_topics = self._pending_topics

        LOG.info("Discovery payloads published")

    # -- Rediscovery ----------------------------------------------------

    def _rediscover(self) -> None:
        """Re-run discovery for guests, ZFS pools, storage, disks, and interfaces.

        Publishes HA discovery configs for any newly found entities and
        primes CPU baselines for new guests.
        """
        if self.en_guests and "guests" not in self.cfg:
            new_guests = LocalCollector.discover_guests()
            old_ids = {(g["type"], g["vmid"]) for g in self.guests}
            added = [g for g in new_guests if (g["type"], g["vmid"]) not in old_ids]
            if added:
                LOG.info(
                    "Rediscovered %d new guest(s): %s",
                    len(added),
                    ", ".join(f"{g['name']}({g['vmid']})" for g in added),
                )
            self.guests = new_guests

        if self.en_zfs and "zfs_pools" not in self.cfg:
            self.zfs_pools = LocalCollector.discover_zfs_pools()

        if self.en_disks or self.en_diskio:
            self.disks = self.collector.discover_disks()
        if self.en_storage:
            self._cached_storage = LocalCollector.storage_status()
        if self.en_network:
            self._net_ifaces = LocalCollector.discover_interfaces()
        if self.en_gpus:
            self.gpus = LocalCollector.discover_gpus()

        self._publish_discovery()

        # Prime CPU baselines for any new guests so the first real
        # poll returns a meaningful delta instead of 0%.
        if self.en_guests:
            for g in self.guests:
                self.collector.guest_status(g["type"], g["vmid"])

    # -- Polling --------------------------------------------------------

    def seed_poll(self) -> None:
        """Silent poll to prime CPU / network / diskio deltas — results are discarded."""
        self.collector.node_status()
        if self.en_network:
            self.collector.network_throughput()
        if self.en_diskio:
            self.collector.disk_io()
        if self.en_guests:
            for g in self.guests:
                self.collector.guest_status(g["type"], g["vmid"])
        LOG.info("Seed poll complete (baselines primed)")

    def poll_and_publish(self) -> None:
        """Collect all enabled metrics and publish to MQTT."""
        n = self.node_name

        # Node state (CPU, mem, swap, uptime, load, freq)
        data = self.collector.node_status()
        if data:
            self.client.publish(f"{self.prefix}/{n}/state", json.dumps(data))

        # Temperatures
        if self.en_temperatures:
            temps = self.collector.node_temperatures()
            if temps:
                self.client.publish(f"{self.prefix}/{n}/temperatures/state", json.dumps(temps))

        # Fan speeds
        if self.en_fans:
            fans = self.collector.fan_speeds()
            if fans:
                self.client.publish(f"{self.prefix}/{n}/fans/state", json.dumps(fans))

        # Network throughput
        if self.en_network:
            net = self.collector.network_throughput()
            for iface, stats in net.items():
                if iface in self._net_ifaces:
                    self.client.publish(
                        f"{self.prefix}/{n}/net/{iface}/state",
                        json.dumps(stats),
                    )

        # Disk I/O
        if self.en_diskio:
            diskio = self.collector.disk_io()
            for ddev, stats in diskio.items():
                self.client.publish(
                    f"{self.prefix}/{n}/diskio/{ddev}/state",
                    json.dumps(stats),
                )

        # Physical disks (temp, health, standby — respects spinup avoidance)
        if self.en_disks:
            for d in self.disks:
                ddev = d["dev"]
                status = self.collector.refresh_disk_status(d)
                payload = {
                    "temperature": status["temperature"],
                    "health": status["health"],
                    "standby": "on" if status["standby"] else "off",
                    "model": d["model"],
                    "serial": d["serial"],
                    "size": d["size"],
                    "dev_path": d["path"],
                }
                self.client.publish(
                    f"{self.prefix}/{n}/disk/{ddev}/state",
                    json.dumps(payload),
                )

        # Guests
        if self.en_guests:
            for g in self.guests:
                data = self.collector.guest_status(g["type"], g["vmid"])
                if data:
                    self.client.publish(
                        f"{self.prefix}/{n}/{g['type']}/{g['vmid']}/state",
                        json.dumps(data),
                    )
                    # Filesystem mount points (only for running guests)
                    if data.get("status") == "running":
                        for fs in self.collector.guest_filesystems(g["type"], g["vmid"]):
                            safe = self._safe_mount(fs["mountpoint"])
                            self.client.publish(
                                f"{self.prefix}/{n}/{g['type']}/{g['vmid']}/fs/{safe}/state",
                                json.dumps(fs),
                            )

        # ZFS pools (includes health)
        if self.en_zfs:
            for pool in self.zfs_pools:
                data = self.collector.zfs_pool(pool)
                if data:
                    self.client.publish(
                        f"{self.prefix}/{n}/zfs/{pool}/state",
                        json.dumps(data),
                    )

        # Storage — refresh via native methods (statvfs, lvs) instead of
        # pvesm status which queries ZFS pools and wakes rotational drives.
        if self.en_storage:
            self.collector.poll_storage(self._cached_storage)
            for s in self._cached_storage:
                if self.en_zfs and s["type"] == "zfspool":
                    continue
                sname = s["name"]
                payload = {
                    "status": s["status"],
                    "total_gb": round(s["total"] * KIB_TO_GB, 2),
                    "used_gb": round(s["used"] * KIB_TO_GB, 2),
                    "avail_gb": round(s["available"] * KIB_TO_GB, 2),
                    "used_pct": s["used_pct"],
                }
                self.client.publish(
                    f"{self.prefix}/{n}/storage/{sname}/state",
                    json.dumps(payload),
                )

        # Pending updates
        if self.en_updates:
            updates = self.collector.pending_updates()
            self.client.publish(
                f"{self.prefix}/{n}/updates/state",
                json.dumps(updates),
            )

        # GPUs
        if self.en_gpus and self.gpus:
            gpu_data = self.collector.gpu_stats(self.gpus)
            for g in self.gpus:
                stats = gpu_data.get(g["id"])
                if stats:
                    self.client.publish(
                        f"{self.prefix}/{n}/gpu/{g['id']}/state",
                        json.dumps(stats),
                    )
