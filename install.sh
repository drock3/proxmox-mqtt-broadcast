#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/proxmox-mqtt"
CONFIG_DIR="/etc/proxmox-mqtt"
SERVICE_USER="proxmox-mqtt"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Escape a string for use as the replacement in a sed s||| expression.
_sed_escape() { printf '%s' "$1" | sed 's/[&|\\]/\\&/g'; }

echo "=== proxmox-mqtt installer ==="

# Ensure python3-venv is available (Proxmox minimal may not have it)
if ! python3 -m venv --help &>/dev/null; then
    echo "Installing python3-venv..."
    apt-get update -qq && apt-get install -y -qq python3-venv
fi

# Create dedicated service user (non-login, no home dir)
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating service user '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# Grant read access to /etc/pve (pmxcfs uses root:www-data ownership)
if ! id -nG "$SERVICE_USER" | grep -qw www-data; then
    echo "Adding '$SERVICE_USER' to www-data group (needed to read VM/CT names)..."
    usermod -aG www-data "$SERVICE_USER"
fi

# Grant access to /dev/dri/* for Intel/AMD GPU monitoring
for grp in video render; do
    if getent group "$grp" &>/dev/null && ! id -nG "$SERVICE_USER" | grep -qw "$grp"; then
        echo "Adding '$SERVICE_USER' to $grp group (needed for GPU monitoring)..."
        usermod -aG "$grp" "$SERVICE_USER"
    fi
done

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"

cp -r "$SCRIPT_DIR/proxmox_mqtt" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    echo ""
    echo "--- MQTT broker configuration ---"
    read -rp "MQTT broker host [192.168.1.x]: " MQTT_HOST
    MQTT_HOST="${MQTT_HOST:-192.168.1.x}"

    read -rp "MQTT broker port [1883]: " MQTT_PORT
    MQTT_PORT="${MQTT_PORT:-1883}"

    read -rp "MQTT username []: " MQTT_USER
    MQTT_USER="${MQTT_USER:-}"

    read -rsp "MQTT password []: " MQTT_PASS
    MQTT_PASS="${MQTT_PASS:-}"
    echo ""

    read -rp "MQTT client ID [proxmox-broadcast]: " MQTT_CLIENT_ID
    MQTT_CLIENT_ID="${MQTT_CLIENT_ID:-proxmox-broadcast}"

    read -rp "MQTT topic prefix [proxmox]: " MQTT_TOPIC_PREFIX
    MQTT_TOPIC_PREFIX="${MQTT_TOPIC_PREFIX:-proxmox}"

    read -rp "MQTT discovery prefix [homeassistant]: " MQTT_DISCOVERY_PREFIX
    MQTT_DISCOVERY_PREFIX="${MQTT_DISCOVERY_PREFIX:-homeassistant}"

    echo ""
    read -rp "Poll interval in seconds [60]: " POLL_INTERVAL
    POLL_INTERVAL="${POLL_INTERVAL:-60}"

    cp "$SCRIPT_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
    chmod 640 "$CONFIG_DIR/config.yaml"
    chown root:"$SERVICE_USER" "$CONFIG_DIR/config.yaml"

    sed -i "s|host: \"192.168.1.x\"|host: \"$(_sed_escape "$MQTT_HOST")\"|"                     "$CONFIG_DIR/config.yaml"
    sed -i "s|port: 1883|port: $(_sed_escape "$MQTT_PORT")|"                                     "$CONFIG_DIR/config.yaml"
    sed -i "s|username: \"\"|username: \"$(_sed_escape "$MQTT_USER")\"|"                          "$CONFIG_DIR/config.yaml"
    sed -i "s|password: \"\"|password: \"$(_sed_escape "$MQTT_PASS")\"|"                          "$CONFIG_DIR/config.yaml"
    sed -i "s|client_id: \"proxmox-broadcast\"|client_id: \"$(_sed_escape "$MQTT_CLIENT_ID")\"|"  "$CONFIG_DIR/config.yaml"
    sed -i "s|topic_prefix: \"proxmox\"|topic_prefix: \"$(_sed_escape "$MQTT_TOPIC_PREFIX")\"|"   "$CONFIG_DIR/config.yaml"
    sed -i "s|discovery_prefix: \"homeassistant\"|discovery_prefix: \"$(_sed_escape "$MQTT_DISCOVERY_PREFIX")\"|" "$CONFIG_DIR/config.yaml"
    sed -i "s|poll_interval: 60|poll_interval: $(_sed_escape "$POLL_INTERVAL")|"                  "$CONFIG_DIR/config.yaml"

    echo "Created $CONFIG_DIR/config.yaml with your settings"
else
    echo "$CONFIG_DIR/config.yaml already exists, not overwriting"
fi

echo "Setting up Python venv..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# Grant the service user read access to the install directory
chown -R root:"$SERVICE_USER" "$INSTALL_DIR"
chmod -R g+rX "$INSTALL_DIR"

cp "$SCRIPT_DIR/proxmox-mqtt.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable proxmox-mqtt

if systemctl is-active --quiet proxmox-mqtt; then
    echo "Service is running — restarting..."
    systemctl restart proxmox-mqtt
fi

# Ensure the drivetemp kernel module is loaded (provides HDD temperatures
# via hwmon without resetting the drive idle timer / preventing spin-down).
if ! lsmod | grep -qw drivetemp; then
    echo "Loading drivetemp kernel module..."
    modprobe drivetemp || echo "  ⚠  Failed to load drivetemp — HDD temps will fall back to smartctl"
fi
if [ ! -f /etc/modules-load.d/drivetemp.conf ] || ! grep -qw drivetemp /etc/modules-load.d/drivetemp.conf; then
    echo "Persisting drivetemp module across reboots..."
    echo "drivetemp" > /etc/modules-load.d/drivetemp.conf
fi

# Detect optional packages and warn about missing ones
echo ""
echo "--- Optional package check ---"
OPTIONAL_PKGS=(
    "smartctl:smartmontools:disk SMART health & temperature"
    "intel_gpu_top:intel-gpu-tools:Intel GPU utilization, power, engine stats"
    "nvidia-smi:nvidia-utils (or driver package):NVIDIA GPU stats"
    "zpool:zfsutils-linux:ZFS pool monitoring"
)
MISSING=0
for entry in "${OPTIONAL_PKGS[@]}"; do
    IFS=':' read -r cmd pkg desc <<< "$entry"
    if ! command -v "$cmd" &>/dev/null; then
        echo "  ⚠  $cmd not found — install '$pkg' for $desc"
        MISSING=$((MISSING + 1))
    fi
done
if [ "$MISSING" -eq 0 ]; then
    echo "  All optional tools detected."
fi

echo ""
echo "Installed. Next steps:"
echo "  1. Review $CONFIG_DIR/config.yaml"
echo "  2. systemctl start proxmox-mqtt"
echo "  3. systemctl status proxmox-mqtt"
