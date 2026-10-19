"""Configuration loading and shared constants."""

import socket

import yaml

BYTES_TO_GB = 1 / 1_073_741_824
BYTES_TO_MB = 1 / 1_048_576
KIB_TO_GB = 1 / 1_048_576  # pvesm reports storage in KiB
CGROUP_ROOT_PATH = "/sys/fs/cgroup"


def load_config(path: str) -> dict:
    """Load YAML config, auto-detecting node name from hostname if missing.

    Args:
        path: Filesystem path to the YAML configuration file.

    Returns:
        Parsed configuration dictionary with at least a ``node`` key.
    """
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if "node" not in cfg:
        cfg["node"] = socket.gethostname()
    return cfg
