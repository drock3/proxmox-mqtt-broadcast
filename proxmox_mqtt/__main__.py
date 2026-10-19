"""Entry point for ``python -m proxmox_mqtt``."""

import logging
import signal
import sys
import time

from .bridge import MQTTBridge
from .collectors import LocalCollector
from .config import load_config

LOG = logging.getLogger(__name__)


def main() -> None:
    """Run the Proxmox -> MQTT bridge."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )

    config_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/proxmox-mqtt/config.yaml"
    LOG.info("Loading config: %s", config_path)
    cfg = load_config(config_path)

    collector = LocalCollector(cfg["node"])
    bridge = MQTTBridge(cfg, collector)

    running = True

    def stop(signum: int, _frame: object) -> None:
        nonlocal running
        LOG.info("Signal %s — shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    bridge.seed_poll()
    bridge.connect()
    time.sleep(2)  # let MQTT connect + publish discovery

    interval = cfg.get("poll_interval", 60)
    LOG.info("Polling every %ds", interval)

    while running:
        bridge.poll_and_publish()
        deadline = time.monotonic() + interval
        while running and time.monotonic() < deadline:
            time.sleep(1)

    bridge.disconnect()
    LOG.info("Stopped")


if __name__ == "__main__":
    main()
