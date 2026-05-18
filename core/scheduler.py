#!/usr/bin/env python3
from fastapi import status
import asyncio
import sqlite3
import logging
import datetime
import httpx
import json
from croniter import croniter
from typing import Dict, Any, List, Optional, Tuple
import os
import random
import sys
import math
import base64

try:
    import tzlocal
    SYSTEM_TZ = tzlocal.get_localzone()
except ImportError:
    logging.warning("tzlocal library not found. Using system's default UTC offset. Install with: pip install tzlocal")
    SYSTEM_TZ = datetime.datetime.now().astimezone().tzinfo

DEFAULT_TASK_DB_PATH = "tasks.db"
TASKS_DATABASE_FILE = DEFAULT_TASK_DB_PATH
CONFIG_FILE_NAME = ".mesh-dash_config"

_scheduler_script_dir_fallback = os.getcwd()
try:
    SCHEDULER_SCRIPT_OWN_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError: 
    SCHEDULER_SCRIPT_OWN_DIR = os.getcwd()

_potential_path_cwd = os.path.join(os.getcwd(), CONFIG_FILE_NAME)
_potential_path_own_dir = os.path.join(SCHEDULER_SCRIPT_OWN_DIR, CONFIG_FILE_NAME)
_potential_path_parent_dir = os.path.join(os.path.dirname(SCHEDULER_SCRIPT_OWN_DIR), CONFIG_FILE_NAME)

if os.path.exists(_potential_path_cwd):
    CONFIG_FILE_PATH = _potential_path_cwd
    _config_path_source_for_file = "current working directory"
elif os.path.exists(_potential_path_own_dir):
    CONFIG_FILE_PATH = _potential_path_own_dir
    _config_path_source_for_file = "scheduler script directory"
elif os.path.exists(_potential_path_parent_dir):
    CONFIG_FILE_PATH = _potential_path_parent_dir
    _config_path_source_for_file = "scheduler script parent directory"
else:
    CONFIG_FILE_PATH = _potential_path_cwd
    _config_path_source_for_file = "current working directory (fallback)"

DEFAULT_SCHEDULER_LOG_LEVEL_STR = "INFO"
DEFAULT_MAIN_APP_WEBSERVER_HOST = "0.0.0.0" 
DEFAULT_MAIN_APP_WEBSERVER_PORT = 8000      
DEFAULT_COMMUNITY_API_ENABLED = False
DEFAULT_HEARTBEAT_INTERVAL_MINUTES = 1
DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED = False
DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED = False
DEFAULT_LOCATION_OFFSET_ENABLED = False
DEFAULT_LOCATION_OFFSET_METERS = 0.0
DEFAULT_SCHEDULER_MAX_RETRIES = 3
DEFAULT_SCHEDULER_RETRY_DELAY_SECONDS = 10
DEFAULT_SCHEDULER_CONNECT_TIMEOUT = 10.0
DEFAULT_SCHEDULER_RW_TIMEOUT = 30.0

SCHEDULER_LOG_LEVEL_STR = DEFAULT_SCHEDULER_LOG_LEVEL_STR
MAIN_APP_API_URL = f"http://{DEFAULT_MAIN_APP_WEBSERVER_HOST}:{DEFAULT_MAIN_APP_WEBSERVER_PORT}" 
COMMUNITY_API_ENABLED = DEFAULT_COMMUNITY_API_ENABLED
HEARTBEAT_INTERVAL = datetime.timedelta(minutes=DEFAULT_HEARTBEAT_INTERVAL_MINUTES)
SEND_LOCAL_NODE_LOCATION = DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED
SEND_OTHER_NODES_LOCATION = DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED
LOCATION_OFFSET_ENABLED = DEFAULT_LOCATION_OFFSET_ENABLED
LOCATION_OFFSET_METERS = DEFAULT_LOCATION_OFFSET_METERS
MAX_RETRIES = DEFAULT_SCHEDULER_MAX_RETRIES
INITIAL_RETRY_DELAY_SECONDS = DEFAULT_SCHEDULER_RETRY_DELAY_SECONDS
CONNECT_TIMEOUT = DEFAULT_SCHEDULER_CONNECT_TIMEOUT
READ_WRITE_TIMEOUT = DEFAULT_SCHEDULER_RW_TIMEOUT
COMMUNITY_API_KEY = ""
EARTH_RADIUS_METERS = 6378137.0
INSTANCE_SECOND_OFFSET = random.uniform(1, 59)

_B64_H = b'aHR0cHM6Ly9tZXNoZGFzaC5jby51ay9jMl9hcGkucGhw'

logger = logging.getLogger("meshtastic_dashboard.scheduler")
if not logger.hasHandlers():
    _initial_handler = logging.StreamHandler(sys.stdout)
    _initial_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - [%(funcName)s] %(message)s')
    _initial_handler.setFormatter(_initial_formatter)
    logger.addHandler(_initial_handler)
    logger.setLevel(logging.INFO) 

logger.info(f"SCHEDULER: Resolved CONFIG_FILE_PATH to: {os.path.abspath(CONFIG_FILE_PATH)} (Source logic: {_config_path_source_for_file})")

_NODE_REGISTRY: Dict[str, Any] = {}

def set_node_registry(registry: Dict[str, Any]) -> None:
    global _NODE_REGISTRY
    _NODE_REGISTRY = registry

def parse_bool_config(value: Any, default_value: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        val_lower = value.strip().lower()
        if val_lower in ('true', '1', 't', 'yes', 'y'):
            return True
        if val_lower in ('false', '0', 'f', 'no', 'n'):
            return False
    return default_value

def load_scheduler_configuration(resolved_config_file_path: str):
    global TASKS_DATABASE_FILE, MAIN_APP_API_URL, SEND_LOCAL_NODE_LOCATION, SEND_OTHER_NODES_LOCATION
    global COMMUNITY_API_ENABLED, HEARTBEAT_INTERVAL
    global LOCATION_OFFSET_ENABLED, LOCATION_OFFSET_METERS
    global MAX_RETRIES, INITIAL_RETRY_DELAY_SECONDS, CONNECT_TIMEOUT, READ_WRITE_TIMEOUT
    global SCHEDULER_LOG_LEVEL_STR, COMMUNITY_API_KEY

    config_keys_defaults = {
        "TASK_DB_PATH": DEFAULT_TASK_DB_PATH,
        "COMMUNITY_API_KEY": "",
        "WEBSERVER_HOST": DEFAULT_MAIN_APP_WEBSERVER_HOST,
        "WEBSERVER_PORT": DEFAULT_MAIN_APP_WEBSERVER_PORT,
        "SEND_LOCAL_NODE_LOCATION": str(DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED).lower(),
        "SEND_OTHER_NODES_LOCATION": str(DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED).lower(),
        "COMMUNITY_API": str(DEFAULT_COMMUNITY_API_ENABLED).lower(),
        "HEARTBEAT_INTERVAL_MINUTES": DEFAULT_HEARTBEAT_INTERVAL_MINUTES,
        "LOCATION_OFFSET_ENABLED": str(DEFAULT_LOCATION_OFFSET_ENABLED).lower(),
        "LOCATION_OFFSET_METERS": str(DEFAULT_LOCATION_OFFSET_METERS),
        "LOG_LEVEL": DEFAULT_SCHEDULER_LOG_LEVEL_STR,
        "SCHEDULER_MAX_RETRIES": DEFAULT_SCHEDULER_MAX_RETRIES,
        "SCHEDULER_RETRY_DELAY_SECONDS": DEFAULT_SCHEDULER_RETRY_DELAY_SECONDS,
        "SCHEDULER_CONNECT_TIMEOUT": DEFAULT_SCHEDULER_CONNECT_TIMEOUT,
        "SCHEDULER_RW_TIMEOUT": DEFAULT_SCHEDULER_RW_TIMEOUT,
    }
    
    file_values = config_keys_defaults.copy()
    found_in_file = {key: False for key in config_keys_defaults}
    sources = {key: "script default" for key in config_keys_defaults}

    if os.path.exists(resolved_config_file_path):
        try:
            with open(resolved_config_file_path, "r") as f:
                for line_number, line in enumerate(f, 1):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key in file_values:
                            file_values[key] = value
                            found_in_file[key] = True
                            sources[key] = f"file ('{os.path.basename(resolved_config_file_path)}')"
        except Exception:
            pass

    if found_in_file.get("TASK_DB_PATH"):
        TASKS_DATABASE_FILE = file_values["TASK_DB_PATH"]
        sources["TASK_DB_PATH"] = f"file ('{os.path.basename(resolved_config_file_path)}')"
    else:
        env_db = os.environ.get("TASK_DB_PATH")
        if env_db:
            TASKS_DATABASE_FILE = env_db
            sources["TASK_DB_PATH"] = "environment variable"
        else:
            TASKS_DATABASE_FILE = DEFAULT_TASK_DB_PATH
            sources["TASK_DB_PATH"] = "script default"

    if found_in_file["LOG_LEVEL"]:
        SCHEDULER_LOG_LEVEL_STR = str(file_values["LOG_LEVEL"]).upper()
    else: 
        env_log_level = os.environ.get("SCHEDULER_LOG_LEVEL", os.environ.get("LOG_LEVEL"))
        if env_log_level:
            SCHEDULER_LOG_LEVEL_STR = env_log_level.upper()
            sources["LOG_LEVEL"] = "environment variable (SCHEDULER_LOG_LEVEL or LOG_LEVEL)"
        else:
            SCHEDULER_LOG_LEVEL_STR = str(config_keys_defaults["LOG_LEVEL"]).upper()
    
    effective_log_level = getattr(logging, SCHEDULER_LOG_LEVEL_STR, logging.INFO)
    if logger.level != effective_log_level:
        logger.setLevel(effective_log_level)
        logging.getLogger("httpx").setLevel(logging.INFO if effective_log_level <= logging.DEBUG else logging.WARNING)

    webserver_host_val = str(file_values["WEBSERVER_HOST"])
    try:
        webserver_port_val = int(file_values["WEBSERVER_PORT"])
    except ValueError:
        webserver_port_val = DEFAULT_MAIN_APP_WEBSERVER_PORT

    env_main_app_url = os.environ.get("MAIN_APP_API_URL")
    if env_main_app_url: 
        MAIN_APP_API_URL = env_main_app_url.rstrip('/')
    else: 
        connect_host = "127.0.0.1" if webserver_host_val == "0.0.0.0" else webserver_host_val
        MAIN_APP_API_URL = f"http://{connect_host}:{webserver_port_val}"

    if found_in_file["SEND_LOCAL_NODE_LOCATION"]:
        SEND_LOCAL_NODE_LOCATION = parse_bool_config(file_values["SEND_LOCAL_NODE_LOCATION"], DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED)
    else:
        env_val = os.environ.get("SEND_LOCAL_NODE_LOCATION")
        if env_val is not None:
            SEND_LOCAL_NODE_LOCATION = parse_bool_config(env_val, DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED)
        else:
            SEND_LOCAL_NODE_LOCATION = DEFAULT_SEND_LOCAL_NODE_LOCATION_SCHED 

    if found_in_file["SEND_OTHER_NODES_LOCATION"]:
        SEND_OTHER_NODES_LOCATION = parse_bool_config(file_values["SEND_OTHER_NODES_LOCATION"], DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED)
    else:
        env_val = os.environ.get("SEND_OTHER_NODES_LOCATION")
        if env_val is not None:
            SEND_OTHER_NODES_LOCATION = parse_bool_config(env_val, DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED)
        else:
            SEND_OTHER_NODES_LOCATION = DEFAULT_SEND_OTHER_NODES_LOCATION_SCHED

    if found_in_file["COMMUNITY_API"]:
        COMMUNITY_API_ENABLED = parse_bool_config(file_values["COMMUNITY_API"], DEFAULT_COMMUNITY_API_ENABLED)
    else:
        env_val = os.environ.get("COMMUNITY_API")
        if env_val is not None:
            COMMUNITY_API_ENABLED = parse_bool_config(env_val, DEFAULT_COMMUNITY_API_ENABLED)
        else:
            COMMUNITY_API_ENABLED = DEFAULT_COMMUNITY_API_ENABLED

    if found_in_file["LOCATION_OFFSET_ENABLED"]:
        LOCATION_OFFSET_ENABLED = parse_bool_config(file_values["LOCATION_OFFSET_ENABLED"], DEFAULT_LOCATION_OFFSET_ENABLED)
    else:
        env_val = os.environ.get("LOCATION_OFFSET_ENABLED")
        if env_val is not None:
            LOCATION_OFFSET_ENABLED = parse_bool_config(env_val, DEFAULT_LOCATION_OFFSET_ENABLED)
        else:
            LOCATION_OFFSET_ENABLED = DEFAULT_LOCATION_OFFSET_ENABLED

    if found_in_file["LOCATION_OFFSET_METERS"]:
        try: LOCATION_OFFSET_METERS = float(file_values["LOCATION_OFFSET_METERS"])
        except ValueError:
            LOCATION_OFFSET_METERS = DEFAULT_LOCATION_OFFSET_METERS
    else:
        env_val = os.environ.get("LOCATION_OFFSET_METERS")
        if env_val is not None:
            try:
                LOCATION_OFFSET_METERS = float(env_val)
            except ValueError:
                LOCATION_OFFSET_METERS = DEFAULT_LOCATION_OFFSET_METERS
        else:
            LOCATION_OFFSET_METERS = DEFAULT_LOCATION_OFFSET_METERS

    COMMUNITY_API_KEY = file_values.get("COMMUNITY_API_KEY", "")

    if found_in_file["HEARTBEAT_INTERVAL_MINUTES"]:
        try: HEARTBEAT_INTERVAL = datetime.timedelta(minutes=int(file_values["HEARTBEAT_INTERVAL_MINUTES"]))
        except ValueError:
            HEARTBEAT_INTERVAL = datetime.timedelta(minutes=DEFAULT_HEARTBEAT_INTERVAL_MINUTES)
    else:
        env_val = os.environ.get("HEARTBEAT_INTERVAL_MINUTES")
        if env_val:
            try:
                HEARTBEAT_INTERVAL = datetime.timedelta(minutes=int(env_val))
            except ValueError:
                HEARTBEAT_INTERVAL = datetime.timedelta(minutes=DEFAULT_HEARTBEAT_INTERVAL_MINUTES)
        else:
            HEARTBEAT_INTERVAL = datetime.timedelta(minutes=DEFAULT_HEARTBEAT_INTERVAL_MINUTES)

    def get_numerical_config(key_name: str, default_value: Any, is_float: bool = False):
        val_to_set = default_value
        final_source = "script default"
        if found_in_file.get(key_name):
            try:
                val_to_set = float(file_values[key_name]) if is_float else int(file_values[key_name])
                final_source = sources[key_name]
            except ValueError:
                final_source = "file (parse error)"
        
        if not found_in_file.get(key_name) or final_source == "file (parse error)":
            env_val_str = os.environ.get(key_name)
            if env_val_str is not None:
                try:
                    val_to_set = float(env_val_str) if is_float else int(env_val_str)
                except ValueError:
                    val_to_set = default_value 
            elif final_source == "file (parse error)": 
                 val_to_set = default_value 

        return val_to_set

    MAX_RETRIES = get_numerical_config("SCHEDULER_MAX_RETRIES", DEFAULT_SCHEDULER_MAX_RETRIES)
    INITIAL_RETRY_DELAY_SECONDS = get_numerical_config("SCHEDULER_RETRY_DELAY_SECONDS", DEFAULT_SCHEDULER_RETRY_DELAY_SECONDS)
    CONNECT_TIMEOUT = get_numerical_config("SCHEDULER_CONNECT_TIMEOUT", DEFAULT_SCHEDULER_CONNECT_TIMEOUT, is_float=True)
    READ_WRITE_TIMEOUT = get_numerical_config("SCHEDULER_RW_TIMEOUT", DEFAULT_SCHEDULER_RW_TIMEOUT, is_float=True)

def offset_lat_lon(lat_deg: float, lon_deg: float, offset_meters: float) -> Tuple[float, float]:
    if offset_meters == 0:
        return lat_deg, lon_deg
    lat_rad = math.radians(lat_deg)
    lon_rad = math.radians(lon_deg)
    angular_distance = offset_meters / EARTH_RADIUS_METERS
    bearing_rad = math.radians(random.uniform(0, 360))
    new_lat_rad = math.asin(
        math.sin(lat_rad) * math.cos(angular_distance) +
        math.cos(lat_rad) * math.sin(angular_distance) * math.cos(bearing_rad)
    )
    new_lon_rad = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(angular_distance) * math.cos(lat_rad),
        math.cos(angular_distance) - math.sin(lat_rad) * math.sin(new_lat_rad)
    )
    new_lat_deg = math.degrees(new_lat_rad)
    new_lon_deg = math.degrees(new_lon_rad)
    new_lon_deg = (new_lon_deg + 540) % 360 - 180
    return new_lat_deg, new_lon_deg

def log_separator(char="=", length=80): return char * length
def log_timestamp(tz: Optional[datetime.tzinfo] = None):
    effective_tz = tz or SYSTEM_TZ 
    try:
        if effective_tz:
             return datetime.datetime.now(effective_tz).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + f" ({effective_tz})"
        else: 
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (Naive/No TZ)"
    except Exception: 
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (Error determining TZ)"

def log_header(text):
    timestamp = log_timestamp()
    return f"\n{log_separator('=')}\n🕒 {timestamp} | 🔍 {text}\n{log_separator('=')}\n"

def format_timedelta(td: datetime.timedelta) -> str:
    try:
        total_seconds = int(td.total_seconds())
        is_past = total_seconds < 0
        if is_past: total_seconds = abs(total_seconds)
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted = ""
        if days > 0: formatted += f"{days}d "
        if days > 0 or hours > 0: formatted += f"{hours:02d}h:"
        if days > 0 or hours > 0 or minutes > 0: formatted += f"{minutes:02d}m:"
        formatted += f"{seconds:02d}s"
        if is_past: return f"Past ({formatted.strip()} ago)"
        return formatted.strip()
    except Exception: return "Error formatting timedelta"

def get_tasks_db_conn_scheduler():
    db_file_path = os.path.join(SCHEDULER_SCRIPT_OWN_DIR, TASKS_DATABASE_FILE)
    if not os.path.exists(db_file_path):
        raise FileNotFoundError(f"Tasks database not found at {db_file_path}")
    try:
        conn = sqlite3.connect(db_file_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
        except Exception: pass
        return conn
    except sqlite3.Error:
        raise

def get_scheduled_tasks() -> list[Dict[str, Any]]:
    tasks = []
    conn = None
    try:
        conn = get_tasks_db_conn_scheduler()
        cursor = conn.cursor()
        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, slotId FROM tasks WHERE enabled = TRUE")
        rows = cursor.fetchall()
        tasks = [dict(row) for row in rows]
    except FileNotFoundError: pass
    except sqlite3.OperationalError as e:
        if "no such column: enabled" in str(e):
            if conn: 
                cursor = conn.cursor()
                cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, slotId FROM tasks")
                tasks = [dict(row) for row in cursor.fetchall()]
    except Exception: pass
    finally:
        if conn: conn.close()
    return tasks

async def trigger_send_message_action(client: httpx.AsyncClient, task: Dict[str, Any]):
    node_id = task.get('nodeId')
    payload_text = task.get('actionPayload')
    slot_id = task.get('slotId') or task.get('slot_id') or 'node_0'

    if not node_id or payload_text is None: 
        return False

    try:
        payload_data = json.loads(payload_text)
        is_json = True
    except (json.JSONDecodeError, TypeError):
        is_json = False
        payload_data = {} 

    message_data: Dict[str, Any]
    api_endpoint_url: str
    base_url = MAIN_APP_API_URL 

    if is_json:
        if 'url' in payload_data and 'block_id' in payload_data and 'prefix' in payload_data:
            api_endpoint_url = f"{base_url}/api/monitor"
            payload_data['node_id'] = node_id
            payload_data['slot_id'] = slot_id
            message_data = payload_data
        else: 
            api_endpoint_url = f"{base_url}/api/messages" 
            if 'message' not in payload_data:
                return False
            payload_data['destination'] = node_id
            payload_data['slot_id'] = slot_id
            message_data = payload_data
    else: 
        api_endpoint_url = f"{base_url}/api/messages" 
        message_data = {"message": payload_text, "destination": node_id, "slot_id": slot_id}

    for attempt in range(MAX_RETRIES):
        current_delay = INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt) 
        if attempt > 0:
            await asyncio.sleep(current_delay)
        try:
            response = await client.post(api_endpoint_url, json=message_data)
            if 200 <= response.status_code < 300:
                return True 
            else: 
                if response.status_code in [408, 429] or response.status_code >= 500: 
                    continue 
                else: 
                    return False 
        except httpx.TimeoutException:
            pass
        except httpx.RequestError: 
            pass
        except Exception: 
            pass 
    
    return False

async def trigger_task_action(client: httpx.AsyncClient, task: Dict[str, Any]):
    ttype = task.get('taskType', '').lower()
    action_successful = False
    if ttype in ['sendmessage', 'message', 'send', 'sendmsg', 'website_monitor', 'websitemonitor']:
        action_successful = await trigger_send_message_action(client, task)
    return action_successful

async def send_heartbeat(client: httpx.AsyncClient):
    status_data, stats_data, all_nodes_data = None, None, None
    fetch_errors: List[str] = [] 
    processed_other_nodes_data: List[Dict[str, Any]] = [] 
    local_node_id = None
    location_disabled_placeholder = "Location Disabled"

    base_url = MAIN_APP_API_URL 
    status_url, stats_url, nodes_url = f"{base_url}/api/status", f"{base_url}/api/stats", f"{base_url}/api/nodes"

    async def fetch_with_retry(url: str, timeout_val: float = 15.0) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        last_err = None
        for attempt in range(MAX_RETRIES):
            if attempt > 0: await asyncio.sleep(INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt))
            try:
                resp = await client.get(url, timeout=timeout_val)
                if 200 <= resp.status_code < 300: return resp.json(), None
                last_err = httpx.HTTPStatusError(f"Status {resp.status_code}", request=resp.request, response=resp)
            except Exception as e: 
                last_err = e
        return None, str(last_err)

    status_data, status_err = await fetch_with_retry(status_url)
    if status_err: fetch_errors.append(status_err)
    if status_data and isinstance(status_data.get("local_node_info"), dict):
        local_node_id = status_data["local_node_info"].get("node_id")

    stats_data, stats_err = await fetch_with_retry(stats_url)
    if stats_err: fetch_errors.append(stats_err)

    all_nodes_data_raw, nodes_err = await fetch_with_retry(nodes_url, timeout_val=20.0) 
    if nodes_err: fetch_errors.append(nodes_err)
    if isinstance(all_nodes_data_raw, dict): 
        all_nodes_data = all_nodes_data_raw
    elif all_nodes_data_raw is not None: 
        fetch_errors.append(f"Invalid data format from {nodes_url}")

    processed_local_node_info = status_data.get('local_node_info') if status_data and isinstance(status_data.get('local_node_info'), dict) else {} 

    def _process_node_location(node_dict: Dict[str, Any], node_id_str: str, is_local: bool):
        should_send_location = SEND_LOCAL_NODE_LOCATION if is_local else SEND_OTHER_NODES_LOCATION
        if not should_send_location:
            node_dict['latitude'] = location_disabled_placeholder
            node_dict['longitude'] = location_disabled_placeholder
            if 'position' in node_dict and isinstance(node_dict['position'], dict):
                node_dict['position']['latitude'] = location_disabled_placeholder
                node_dict['position']['longitude'] = location_disabled_placeholder
                node_dict['position']['latitudeI'] = None 
                node_dict['position']['longitudeI'] = None
        elif LOCATION_OFFSET_ENABLED and LOCATION_OFFSET_METERS > 0:
            current_lat = node_dict.get('latitude')
            current_lon = node_dict.get('longitude')
            if isinstance(current_lat, (float, int)) and isinstance(current_lon, (float, int)):
                try:
                    offset_lat, offset_lon = offset_lat_lon(float(current_lat), float(current_lon), LOCATION_OFFSET_METERS)
                    node_dict['latitude'] = offset_lat
                    node_dict['longitude'] = offset_lon
                    if 'position' in node_dict and isinstance(node_dict['position'], dict):
                        node_dict['position']['latitude'] = offset_lat
                        node_dict['position']['longitude'] = offset_lon
                        node_dict['position']['latitudeI'] = None 
                        node_dict['position']['longitudeI'] = None
                except Exception:
                    pass
        return node_dict 

    if processed_local_node_info and local_node_id: 
        processed_local_node_info = _process_node_location(processed_local_node_info, local_node_id, is_local=True)

    if isinstance(all_nodes_data, dict):
        for node_id_key, node_data_val in all_nodes_data.items():
            if node_id_key == local_node_id: continue 
            if isinstance(node_data_val, dict):
                processed_node_item = node_data_val.copy() 
                processed_node_item = _process_node_location(processed_node_item, node_id_key, is_local=False)
                processed_other_nodes_data.append(processed_node_item)

    payload = {
        "type": "heartbeat_v2",
        "fetched_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "local_node_info": processed_local_node_info if processed_local_node_info else None, 
        "stats_data": stats_data, 
        "other_nodes_data": processed_other_nodes_data, 
        "fetch_errors": fetch_errors if fetch_errors else None 
    }

    _remote = base64.b64decode(_B64_H).decode('utf-8')
    for attempt in range(MAX_RETRIES): 
        current_delay = INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt)
        if attempt > 0:
            await asyncio.sleep(current_delay)
        try:
            hb_headers = {}
            if COMMUNITY_API_KEY:
                hb_headers["X-Api-Key"] = COMMUNITY_API_KEY
            if local_node_id:
                hb_headers["X-Node-Id"] = local_node_id
            response = await client.post(_remote, json=payload, headers=hb_headers)
            if 200 <= response.status_code < 300:
                return True 
            else:
                if response.status_code in [408, 429] or response.status_code >= 500:
                    continue
                else:
                    return False
        except Exception:
            pass
    
    return False

async def check_and_trigger_tasks(client: httpx.AsyncClient, system_tz_val: datetime.tzinfo, last_check_time_utc_val: datetime.datetime):
    current_check_time_local_for_display = datetime.datetime.now(system_tz_val)
    current_check_time_utc_val = current_check_time_local_for_display.astimezone(datetime.timezone.utc)
    last_check_time_local_for_croniter = last_check_time_utc_val.astimezone(system_tz_val)

    tasks = get_scheduled_tasks() 
    triggered_count = 0

    if not tasks:
        return triggered_count, current_check_time_utc_val 

    for task in tasks:
        cron_str = task.get('cronString')
        if not cron_str:
            continue
        try:
            itr = croniter(cron_str, start_time=last_check_time_local_for_croniter, ret_type=datetime.datetime, is_prev=False)
            next_scheduled_time_local = itr.get_next() 

            if next_scheduled_time_local.tzinfo is None and system_tz_val: 
                next_scheduled_time_local = next_scheduled_time_local.replace(tzinfo=system_tz_val) 
            
            next_scheduled_time_utc = next_scheduled_time_local.astimezone(datetime.timezone.utc)

            if last_check_time_utc_val < next_scheduled_time_utc <= current_check_time_utc_val:
                success = await trigger_task_action(client, task) 
                if success: triggered_count += 1

        except ValueError: pass
        except Exception: pass
    
    return triggered_count, current_check_time_utc_val 

async def run_scheduler_periodically():
    load_scheduler_configuration(CONFIG_FILE_PATH) 

    if not SYSTEM_TZ: 
        return
    
    initial_jitter_val = random.uniform(0.5, 5.0)
    await asyncio.sleep(initial_jitter_val)

    last_heartbeat_time_utc = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    last_check_time_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)

    while True:
        actual_check_time_utc_for_this_cycle = datetime.datetime.now(datetime.timezone.utc) 

        try:
            try:
                load_scheduler_configuration(CONFIG_FILE_PATH) 
            except Exception:
                pass
            
            current_timeout_config = httpx.Timeout(READ_WRITE_TIMEOUT, connect=CONNECT_TIMEOUT)
            async with httpx.AsyncClient(timeout=current_timeout_config) as client_for_this_cycle:

                now_naive_for_calc = datetime.datetime.now() 
                target_second_of_minute = int(INSTANCE_SECOND_OFFSET) 
                target_microsecond_of_second = int((INSTANCE_SECOND_OFFSET - target_second_of_minute) * 1_000_000)
                
                next_potential_target_naive = now_naive_for_calc.replace(
                    second=target_second_of_minute, 
                    microsecond=target_microsecond_of_second, 
                    tzinfo=None 
                )
                
                if next_potential_target_naive <= now_naive_for_calc:
                    target_time_naive = next_potential_target_naive + datetime.timedelta(minutes=1)
                else:
                    target_time_naive = next_potential_target_naive
                
                wait_duration_seconds = (target_time_naive - now_naive_for_calc).total_seconds()

                if wait_duration_seconds < 0: 
                    wait_duration_seconds = 0.1
                
                await asyncio.sleep(wait_duration_seconds)

                actual_check_time_local = datetime.datetime.now(SYSTEM_TZ) 
                actual_check_time_utc_for_this_cycle = actual_check_time_local.astimezone(datetime.timezone.utc) 
                
                time_since_last_heartbeat = actual_check_time_utc_for_this_cycle - last_heartbeat_time_utc
                if time_since_last_heartbeat >= HEARTBEAT_INTERVAL:
                    success = await send_heartbeat(client_for_this_cycle)
                    if success:
                        last_heartbeat_time_utc = actual_check_time_utc_for_this_cycle 

                tasks_triggered_this_cycle, _ = await check_and_trigger_tasks(
                    client_for_this_cycle, 
                    SYSTEM_TZ, 
                    last_check_time_utc 
                )
                
                last_check_time_utc = actual_check_time_utc_for_this_cycle

                summary_time_local = datetime.datetime.now(SYSTEM_TZ)
                cycle_duration = (summary_time_local - actual_check_time_local).total_seconds()
            
        except asyncio.CancelledError:
            break
        except FileNotFoundError:
            break 
        except httpx.ConnectError:
            await asyncio.sleep(60) 
            last_check_time_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1) 
        except Exception:
            await asyncio.sleep(60) 
            last_check_time_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)

if __name__ == '__main__':
    try:
        asyncio.run(run_scheduler_periodically())
    except KeyboardInterrupt:
        pass
    except Exception:
        sys.exit(1)