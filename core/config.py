"""
Configuration module for Mesh Dashboard.
Extracted from meshtastic_dashboard.py
"""
import re
import os
import logging
import secrets
from typing import Any, Dict, Set

# Default configuration values

DEFAULT_TARGET_HOST = "192.168.0.0"
DEFAULT_TARGET_PORT = 4403
DEFAULT_LOG_LEVEL_STR = "INFO"
DEFAULT_WEBSERVER_PORT = 8181
DEFAULT_WEBSERVER_HOST = "0.0.0.0"
DEFAULT_DB_PATH = "data/meshtastic_data.db"
DEFAULT_TASK_DB_PATH = "data/tasks.db"
DEFAULT_MAX_PACKETS_MEMORY = 200
DEFAULT_AVERAGE_METRICS_HISTORY_DAYS = 1
DEFAULT_AUTH_SECRET_KEY = secrets.token_hex(32)
DEFAULT_AUTH_TOKEN_EXPIRE_MINUTES = 10080

# Configuration file path helpers

def _resolve_base():
    return "https://meshdash.co.uk"

def _resolve_community():
    return _resolve_base() + "/c2_com_api.php"

def _resolve_heartbeat():
    return _resolve_base() + "/c2_api.php"

def _resolve_versions():
    return _resolve_base() + "/versions"

CONFIG_FILE_NAME = ".mesh-dash_config"

try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    SCRIPT_DIR = os.getcwd()

CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, "data", CONFIG_FILE_NAME)
# Fallback: check legacy root location if data/ doesn't have it
if not os.path.exists(CONFIG_FILE_PATH):
    _legacy_config_path = os.path.join(SCRIPT_DIR, CONFIG_FILE_NAME)
    if os.path.exists(_legacy_config_path):
        CONFIG_FILE_PATH = _legacy_config_path

# Configuration loader

def load_configuration(config_path: str) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "MESHTASTIC_HOST": DEFAULT_TARGET_HOST,
        "MESHTASTIC_PORT": DEFAULT_TARGET_PORT,
        "MESHTASTIC_CONNECTION_TYPE": "SERIAL",
        "MESHTASTIC_SERIAL_PORT": "",
        "MESHTASTIC_BLE_MAC": "",
        "WEBSERVER_HOST": DEFAULT_WEBSERVER_HOST,
        "WEBSERVER_PORT": DEFAULT_WEBSERVER_PORT,
        "NETWORK_WEBSERVER_PORT": DEFAULT_WEBSERVER_PORT,
        "DB_PATH": DEFAULT_DB_PATH,
        "TASK_DB_PATH": DEFAULT_TASK_DB_PATH,
        "MAX_PACKETS_MEMORY": DEFAULT_MAX_PACKETS_MEMORY,
        "HISTORY_DAYS": DEFAULT_AVERAGE_METRICS_HISTORY_DAYS,
        "LOG_LEVEL": DEFAULT_LOG_LEVEL_STR,
        "COMMUNITY_API": False,
        "COMMUNITY_API_KEY": "YOUR_SUPER_SECRET_API_KEY_REPLACE_ME",
        "HEARTBEAT_INTERVAL_MINUTES": 1,  # heartbeat always active — hardcoded internally
        "SEND_LOCAL_NODE_LOCATION": True,
        "SEND_OTHER_NODES_LOCATION": True,
        "LOCATION_OFFSET_ENABLED": False,
        "LOCATION_OFFSET_METERS": 0.0,
        "AUTH_SECRET_KEY": DEFAULT_AUTH_SECRET_KEY,
        "AUTH_TOKEN_EXPIRE_MINUTES": DEFAULT_AUTH_TOKEN_EXPIRE_MINUTES,
        "SCHEDULER_MAX_RETRIES": 3,
        "SCHEDULER_RETRY_DELAY_SECONDS": 10,
        "SCHEDULER_CONNECT_TIMEOUT": 10.0,
        "SCHEDULER_RW_TIMEOUT": 30.0,
        "C2_ACCESS_LEVEL": "read",
        "REMOTE_C2": False,  # Remote C2 access toggle (heartbeat always active)
        "INITIAL_ADMIN_USERNAME": None,
        "INITIAL_ADMIN_PASSWORD": None,
        "PUBLIC_MODE": True,
    }
    if not os.path.exists(config_path):
        return config
    try:
        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key not in config:
                    continue
                try:
                    current_value = config[key]
                    if isinstance(current_value, bool):
                        config[key] = value.lower() in ("true", "1", "yes", "on")
                    elif current_value is None:
                        config[key] = value if value else None
                    elif isinstance(current_value, int):
                        config[key] = int(float(value))
                    elif isinstance(current_value, float):
                        config[key] = float(value)
                    else:
                        config[key] = value
                except (ValueError, Exception):
                    config[key] = value
    except Exception as e:
        logging.getLogger("core.config").error(f"Error loading config: {e}")
    return config


# Config file path (from system.py)

DEFAULT_DASH_CONFIG_PATH = "data/.mesh-dash_config"

DASH_CONFIG_PATH = os.environ.get("DASH_CONFIG_PATH", DEFAULT_DASH_CONFIG_PATH)
ABS_DASH_CONFIG_PATH = os.path.abspath(DASH_CONFIG_PATH)

_config_logger = logging.getLogger("meshtastic_dashboard.config")
_config_logger.info(f"System module using MeshDash config path: {ABS_DASH_CONFIG_PATH}")


# Config file read/write (from system.py)

def read_dash_config(filepath: str) -> Dict[str, str]:
    """Reads the MeshDash .mesh-dash_config file and returns a dictionary."""
    config = {}
    abs_filepath = os.path.abspath(filepath)
    if not os.path.exists(abs_filepath):
        _config_logger.warning(f"MeshDash config file not found: {abs_filepath}")
        return {}
    try:
        with open(abs_filepath, 'r', encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                match = re.match(r'^([^=\s]+)\s*=\s*("?)(.*?)\s*$', line)
                if match:
                    key = match.group(1)
                    value = match.group(3)
                    config[key] = value
                else:
                    _config_logger.warning(f"Could not parse config line in {abs_filepath}: {line}")
    except IOError as e:
        _config_logger.error(f"IOError reading MeshDash config file {abs_filepath}: {e}", exc_info=True)
        return {}
    except Exception as e:
        _config_logger.error(f"Unexpected error reading MeshDash config file {abs_filepath}: {e}", exc_info=True)
        return {}
    _config_logger.debug(f"Successfully read {len(config)} keys from {abs_filepath}")
    return config


def write_dash_config(filepath: str, config_data_to_update: Dict[str, str]) -> Set[str]:
    """Writes updated key-value pairs back to the MeshDash config file."""
    lines_to_write = []
    updated_keys = set(config_data_to_update.keys())
    keys_actually_written = set()
    abs_filepath = os.path.abspath(filepath)
    _config_logger.info(f"Attempting to write updates for keys {updated_keys} to {abs_filepath}")

    config_dir = os.path.dirname(abs_filepath)
    if not os.path.exists(config_dir):
        try:
            os.makedirs(config_dir, exist_ok=True)
        except Exception as e:
            _config_logger.error(f"Failed to create directory for config file {config_dir}: {e}")
            raise IOError(f"Failed to create directory for config file: {e}")

    try:
        if os.path.exists(abs_filepath):
            with open(abs_filepath, 'r', encoding="utf-8") as f_read:
                original_lines = f_read.readlines()
        else:
            original_lines = []

        for line in original_lines:
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith('#'):
                lines_to_write.append(line)
                continue
            match = re.match(r'^([^=\s]+)\s*=', stripped_line)
            if match:
                key = match.group(1)
                if key in updated_keys:
                    new_value = str(config_data_to_update[key])
                    if key in ["INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_PASSWORD", "DB_PATH", "MESHTASTIC_HOST"]:
                        formatted_line = key + '="' + new_value + '"\n'
                    elif new_value.lower() in ["true", "false"]:
                        formatted_line = key + '=' + new_value.lower() + '\n'
                    elif re.fullmatch(r"[-+]?\d*\.\d+|\d+", new_value) and not (' ' in new_value or '#' in new_value):
                        formatted_line = key + '=' + new_value + '\n'
                    elif ' ' in new_value or not new_value or '#' in new_value:
                        formatted_line = key + '="' + new_value + '"\n'
                    else:
                        formatted_line = key + '=' + new_value + '\n'
                    lines_to_write.append(formatted_line)
                    keys_actually_written.add(key)
                else:
                    lines_to_write.append(line)
            else:
                lines_to_write.append(line)

        new_keys_to_add = updated_keys - keys_actually_written
        if new_keys_to_add:
            if lines_to_write and lines_to_write[-1].strip() != "":
                lines_to_write.append("\n")
            for key in sorted(list(new_keys_to_add)):
                new_value = str(config_data_to_update[key])
                if key in ["INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_PASSWORD", "DB_PATH", "MESHTASTIC_HOST"]:
                    formatted_line = key + '="' + new_value + '"\n'
                elif new_value.lower() in ["true", "false"]:
                    formatted_line = key + '=' + new_value.lower() + '\n'
                elif re.fullmatch(r"[-+]?\d*\.\d+|\d+", new_value) and not (' ' in new_value or '#' in new_value):
                    formatted_line = key + '=' + new_value + '\n'
                elif ' ' in new_value or not new_value or '#' in new_value:
                    formatted_line = key + '="' + new_value + '"\n'
                else:
                    formatted_line = key + '=' + new_value + '\n'
                lines_to_write.append(formatted_line)
                keys_actually_written.add(key)

        with open(abs_filepath, 'w', encoding="utf-8") as f_write:
            f_write.writelines(lines_to_write)

        _config_logger.info(f"Successfully wrote config updates to {abs_filepath} for keys: {keys_actually_written}")
        return keys_actually_written

    except IOError as e:
        _config_logger.error(f"IOError writing MeshDash config file {abs_filepath}: {e}", exc_info=True)
        raise
    except Exception as e:
        _config_logger.error(f"Unexpected error writing MeshDash config file {abs_filepath}: {e}", exc_info=True)
        raise
