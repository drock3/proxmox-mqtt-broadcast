# Proxmox MQTT Broadcast

A lightweight bridge that runs directly on a Proxmox VE host and publishes node, VM/container, ZFS, storage, disk, network, and hardware metrics to an MQTT broker. Home Assistant auto-discovers every entity via [MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) — no Proxmox API tokens required.

## What Gets Published

| Category | Metrics |
|---|---|
| **Node** | CPU %, memory used/total (GB), memory %, swap used/total (GB), swap %, uptime (hours), load average (1/5/15 min), CPU frequency (MHz) |
| **Temperatures** | CPU / board thermal zones (°C) |
| **Fan Speeds** | RPM per fan from hwmon |
| **QEMU VMs** | Status, CPU %, memory used (GB), memory % |
| **LXC Containers** | Status, CPU %, memory used (GB), memory % |
| **Network Interfaces** | RX / TX bytes per second (physical, bridge, and bond interfaces) |
| **Disk I/O** | Read / write bytes per second per block device |
| **Physical Disks** | Temperature (°C), SMART health, standby state, model, serial, size |
| **ZFS Pools** | Used %, health state (ONLINE/DEGRADED/FAULTED) |
| **Storage** (all `pvesm` types) | Status, total/used/available (GB), used % |
| **GPUs** | NVIDIA: utilization %, memory-bandwidth %, encoder/decoder %, VRAM used/free/total (MB) + %, temperature (°C), power draw/limit (W) + %, fan %, GPU/memory clock (MHz), PCIe gen/width, performance state, compute process count and VRAM. AMD: utilization %, VRAM used/total (MB), VRAM %, temperature (°C), power (W), GPU/memory clock (MHz). Intel: utilization %, RC6 residency %, power (W), per-engine busy %, IMC read/write bandwidth, clock frequencies (requires `intel-gpu-tools`) |
| **Guest Filesystems** | Per-mount-point disk usage for VMs (via QEMU guest agent) and LXC containers: total/used/available (GB), used % |
| **Pending Updates** | Available update count, security update count, reboot required (reads cached apt data — zero I/O, never runs apt) |

Guests, ZFS pools, storage, disks, GPUs, and network interfaces are **auto-discovered** by default. You can pin specific items in the config if you prefer.

> **Disk spinup protection:** Rotational disks (HDDs) are checked for standby/sleep state before querying SMART data. If a disk is sleeping, temperature and health report as `null` (shown as "unknown" in HA) and the standby sensor shows "on". All `smartctl` calls use `--nocheck standby` to guarantee no spinup.

---

## Proxmox Side — Installation

### Prerequisites

- Proxmox VE 7.x or 8.x
- Python 3 (ships with Proxmox)
- Network access from the Proxmox host to your MQTT broker
- `smartmontools` (for disk health/temperature — `apt install smartmontools`)
- Optional: `intel-gpu-tools` for Intel GPU utilization, power, and engine stats (`apt install intel-gpu-tools`)
- Optional: `drivetemp` kernel module for HDD temps via hwmon (`modprobe drivetemp`)

### Quick Install

SSH into your Proxmox host and run:

```bash
# Clone or copy the project files to the host
cd /tmp
git clone <repo-url> proxmox-broadcast   # or scp the files over
cd proxmox-broadcast

# Run the installer (creates venv, installs deps, enables the service)
bash install.sh
```

The installer does the following:

1. Installs `python3-venv` if missing.
2. Creates a dedicated `proxmox-mqtt` system user (non-login, no home directory).
3. Copies the package to `/opt/proxmox-mqtt/` and creates a Python virtual environment there.
4. Creates `/etc/proxmox-mqtt/config.yaml` from the example (only on first install), owned `root:proxmox-mqtt` with mode `640`.
5. Installs and enables the `proxmox-mqtt` systemd service (runs as the `proxmox-mqtt` user with hardened sandbox settings).

### Configure

Edit the config file:

```bash
nano /etc/proxmox-mqtt/config.yaml
```

At minimum, set your MQTT broker address and credentials:

```yaml
mqtt:
  host: "192.168.1.100"       # your MQTT broker IP
  port: 1883
  username: "mqtt_user"
  password: "mqtt_pass"
  client_id: "proxmox-broadcast"
  topic_prefix: "proxmox"
  discovery_prefix: "homeassistant"

poll_interval: 60              # how often to publish (seconds)
```

#### Optional: MQTT TLS

To encrypt the MQTT connection, add a `tls` block under `mqtt`:

```yaml
mqtt:
  host: "broker.example.com"
  port: 8883
  username: "mqtt_user"
  password: "mqtt_pass"
  tls:
    enabled: true
    ca_certs: "/etc/ssl/certs/ca-certificates.crt"  # CA bundle
    # certfile: "/path/to/client.crt"  # optional, for mutual TLS
    # keyfile: "/path/to/client.key"   # optional, for mutual TLS
    # insecure: false                   # skip server cert verification
```

Omit the `tls` block entirely (or set `enabled: false`) to connect over plaintext.

The `node` name is auto-detected from the hostname. Override it if needed:

```yaml
node: "pve1"
```

By default every guest, ZFS pool, and storage target is auto-discovered. To limit what gets broadcast, list them explicitly:

```yaml
guests:
  - vmid: 100
    type: qemu
    name: MyVM
  - vmid: 101
    type: lxc
    name: MyContainer

zfs_pools:
  - rpool
```

Every metric category can be individually enabled or disabled (all default to `true`):

```yaml
enabled:
  guests: true          # QEMU VMs and LXC containers
  zfs: true             # ZFS pool usage and health
  storage: true         # Proxmox storage (pvesm)
  temperatures: true    # CPU / board thermal zones
  fans: true            # Fan RPMs from hwmon
  network: true         # Per-interface RX/TX throughput
  disk_io: true         # Per-disk read/write throughput
  disks: true           # Physical disk temp, SMART health, model/serial
  gpus: true            # GPU stats (Intel, AMD, NVIDIA)
  updates: true         # Pending apt updates & reboot required (zero I/O)
```

### Start the Service

```bash
systemctl start proxmox-mqtt
systemctl status proxmox-mqtt
```

Check the logs:

```bash
journalctl -u proxmox-mqtt -f
```

The service is already enabled on boot by the installer. To disable it:

```bash
systemctl disable proxmox-mqtt
```

### Updating

To update to a newer version, pull the latest code and re-run the installer. Your config file is preserved automatically.

```bash
cd /tmp
git clone <repo-url> proxmox-broadcast   # or scp the updated files over
cd proxmox-broadcast
bash install.sh
systemctl restart proxmox-mqtt
```

The installer skips config creation if `/etc/proxmox-mqtt/config.yaml` already exists, so your settings are never overwritten. It will update the Python package, rebuild the venv dependencies, and reinstall the systemd service.

If you copied the files manually instead of using git:

```bash
# Copy the updated package into place
cp -r proxmox_mqtt /opt/proxmox-mqtt/
cp requirements.txt /opt/proxmox-mqtt/

# Reinstall dependencies (in case requirements changed)
/opt/proxmox-mqtt/venv/bin/pip install --quiet -r /opt/proxmox-mqtt/requirements.txt

# Fix ownership and restart
chown -R root:proxmox-mqtt /opt/proxmox-mqtt
chmod -R g+rX /opt/proxmox-mqtt
systemctl restart proxmox-mqtt
```

### Manual Install (Without the Script)

If you prefer not to use `install.sh`:

```bash
# Create a dedicated service user
useradd --system --no-create-home --shell /usr/sbin/nologin proxmox-mqtt

mkdir -p /opt/proxmox-mqtt /etc/proxmox-mqtt

cp -r proxmox_mqtt /opt/proxmox-mqtt/
cp requirements.txt /opt/proxmox-mqtt/
cp config.example.yaml /etc/proxmox-mqtt/config.yaml
chown root:proxmox-mqtt /etc/proxmox-mqtt/config.yaml
chmod 640 /etc/proxmox-mqtt/config.yaml

python3 -m venv /opt/proxmox-mqtt/venv
/opt/proxmox-mqtt/venv/bin/pip install -r /opt/proxmox-mqtt/requirements.txt

chown -R root:proxmox-mqtt /opt/proxmox-mqtt
chmod -R g+rX /opt/proxmox-mqtt

cp proxmox-mqtt.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now proxmox-mqtt
```

> **Running as root:** If you need root for tools like `smartctl` or `zpool` and don't want to configure capabilities, comment out the `User=` and `Group=` lines in the service file.

---

## Home Assistant Side — Setup

### 1. Install an MQTT Broker

If you don't already have one, install the **Mosquitto** add-on:

1. Go to **Settings → Add-ons → Add-on Store**.
2. Search for **Mosquitto broker** and install it.
3. Start the add-on.
4. Create a dedicated MQTT user under **Settings → People → Users** (or use an existing one).

### 2. Enable the MQTT Integration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **MQTT** and add it.
3. Enter your broker details (if using the Mosquitto add-on, it auto-configures).

### 3. Verify Entities Appear

Once the Proxmox service connects and publishes discovery messages, entities show up automatically:

1. Go to **Settings → Devices & Services → MQTT**.
2. You should see devices named **Proxmox \<node\>**, plus one device per VM/container, ZFS pool, and storage target.
3. Each device contains sensors for CPU, memory, status, etc.

No manual YAML configuration is needed on the HA side.

### 4. Rediscover New Guests

When you create a new VM or container on Proxmox, you can trigger re-discovery without restarting the service:

- In Home Assistant, find the **Rediscover** button entity under the Proxmox node device and press it.
- Or publish any message to the MQTT topic `proxmox/<node>/rediscover`.

### 5. Build a Dashboard

All sensors are standard HA entities. Add them to any dashboard using cards like:

- **Gauge** — for CPU % or memory %
- **Entities** — for a quick overview of all VMs
- **Glance** — for a compact multi-sensor view

Example entity IDs (pattern: `sensor.pve_<node>_<metric>`):

```
# Node
sensor.pve_mynode_cpu
sensor.pve_mynode_mem_used
sensor.pve_mynode_swap_pct
sensor.pve_mynode_uptime
sensor.pve_mynode_load_1m
sensor.pve_mynode_cpu_freq
sensor.pve_mynode_temp_x86_pkg_temp
sensor.pve_mynode_fan_cpu_fan1

# Guests
sensor.pve_mynode_qemu_100_status
sensor.pve_mynode_lxc_101_cpu

# Network
sensor.pve_mynode_net_eno1_rx
sensor.pve_mynode_net_eno1_tx

# Disk I/O
sensor.pve_mynode_diskio_sda_read
sensor.pve_mynode_diskio_nvme0n1_write

# Physical disks
sensor.pve_mynode_disk_sda_temp
sensor.pve_mynode_disk_sda_health
sensor.pve_mynode_disk_sda_standby

# ZFS & Storage
sensor.pve_mynode_zfs_rpool_used
sensor.pve_mynode_zfs_rpool_health
sensor.pve_mynode_storage_local_pct

# GPUs
sensor.pve_mynode_gpu_pci_0000_00_02_0_clock_mhz
sensor.pve_mynode_gpu_nvidia_0_utilization_pct
sensor.pve_mynode_gpu_nvidia_0_temperature_c

# Guest Filesystems
sensor.pve_mynode_qemu_100_fs_root_used_pct
sensor.pve_mynode_lxc_101_fs_root_total

# Pending Updates
sensor.pve_mynode_updates_available
sensor.pve_mynode_security_updates
sensor.pve_mynode_reboot_required
```

---

## MQTT Topic Reference

All topics are published under `{topic_prefix}/{node}/`:

| Topic | Payload |
|---|---|
| `proxmox/{node}/state` | `{"cpu": 12.3, "mem_used_gb": 8.5, "mem_total_gb": 32.0, "mem_used_pct": 26.6, "swap_used_gb": 0.5, "swap_total_gb": 8.0, "swap_used_pct": 6.3, "uptime": 123.45, "load_1m": 0.5, "load_5m": 0.3, "load_15m": 0.2, "cpu_freq_mhz": 3200}` |
| `proxmox/{node}/temperatures/state` | `{"x86_pkg_temp": 45.0, "acpitz": 27.8}` |
| `proxmox/{node}/fans/state` | `{"cpu_fan1": 1200, "sys_fan1": 800}` |
| `proxmox/{node}/qemu/{vmid}/state` | `{"status": "running", "cpu": 5.1, "mem_used_gb": 2.0, "mem_used_pct": 50.0}` |
| `proxmox/{node}/lxc/{vmid}/state` | `{"status": "running", "cpu": 1.2, "mem_used_gb": 0.5, "mem_used_pct": 25.0}` |
| `proxmox/{node}/net/{iface}/state` | `{"rx_mb_sec": 0.98, "tx_mb_sec": 0.49}` |
| `proxmox/{node}/diskio/{dev}/state` | `{"read_mb_sec": 1.95, "write_mb_sec": 0.98}` |
| `proxmox/{node}/disk/{dev}/state` | `{"temperature": 35.0, "health": "PASSED", "standby": "off", "model": "WDC WD40EFRX", "serial": "WD-ABC123", "size": "3.6T"}` |
| `proxmox/{node}/zfs/{pool}/state` | `{"used_pct": 42.3, "health": "ONLINE"}` |
| `proxmox/{node}/storage/{name}/state` | `{"status": "active", "total_gb": 100.0, "used_gb": 45.0, "avail_gb": 55.0, "used_pct": 45.0}` |
| `proxmox/{node}/gpu/{id}/state` | NVIDIA: `{"utilization_pct": 30.0, "memory_used_mb": 2048, "memory_total_mb": 8192, "encoder_pct": 0.0, "power_w": 24.1, "power_limit_w": 50.0, "pcie_gen": 4, "performance_state": "P0", "processes": 1, ...}` · AMD: `{"utilization_pct": 30.0, "memory_total_mb": 8192, "memory_used_mb": 2048, ...}` · Intel: `{"utilization_pct": 15.0, "clock_mhz": 1100, "rc6_pct": 85.0, "power_w": 4.5, "engine_render_3d_pct": 12.0, "engine_video_pct": 0.0, "imc_reads_mib_s": 150.0, ...}` |
| `proxmox/availability` | `online` / `offline` (LWT) |

> **Note:** For disks in standby, `temperature` and `health` will be `null` and `standby` will be `"on"`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No entities in HA | Check that the MQTT integration is active and the `discovery_prefix` in `config.yaml` matches HA's setting (default `homeassistant`). |
| Service won't start | Run `journalctl -u proxmox-mqtt -e` and check for config/connection errors. |
| CPU always shows 0% | The first poll primes the baseline delta — values appear after the second poll cycle. Same applies to network throughput and disk I/O. |
| Stale / missing guests | Press the **Rediscover** button in HA or restart the service. Also re-discovers new disks and network interfaces. |
| MQTT auth errors | Verify `username`/`password` in `config.yaml` match the broker credentials. |
| Disk temp shows unknown | The disk is in standby/sleep. This is intentional — querying SMART would spin it up. It will update when the disk wakes. |
| No temperature sensors | Ensure `lm-sensors` / thermal zone modules are loaded. For disk temps, the `drivetemp` kernel module helps (`modprobe drivetemp`). |
| No fan speed sensors | Not all hardware exposes fan data via hwmon. Check `ls /sys/class/hwmon/*/fan*_input`. |
| Intel GPU only shows clocks | Install `intel-gpu-tools` (`apt install intel-gpu-tools`). The service must run as root (or with `CAP_PERFMON`) for `intel_gpu_top` to read perf counters. |
| SMART health missing | Install `smartmontools` (`apt install smartmontools`). If running as the `proxmox-mqtt` user, ensure `smartctl` has the necessary permissions (e.g., setuid or `CAP_SYS_RAWIO` capability). |

## Uninstall

```bash
systemctl disable --now proxmox-mqtt
rm /etc/systemd/system/proxmox-mqtt.service
systemctl daemon-reload
rm -rf /opt/proxmox-mqtt /etc/proxmox-mqtt
userdel proxmox-mqtt 2>/dev/null || true
```
