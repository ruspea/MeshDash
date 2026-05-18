# Shared globals for extracted modules.
# Populated by meshtastic_dashboard.py at startup before any route handler runs.
# Extracted modules import from here to avoid circular imports.

# --- Configuration ---
loaded_config = {}
PUBLIC_MODE = True
AUTH_SECRET_KEY = ""
AUTH_TOKEN_EXPIRE_MINUTES = 10080
COMMUNITY_API_KEY = ""
# Connection globals set at startup and hot-reloaded on setup
TARGET_HOST = ""
TARGET_PORT = 4403
MESHTASTIC_CONNECTION_TYPE = "SERIAL"
MESHTASTIC_SERIAL_PORT = ""
MESHTASTIC_BLE_MAC = ""
CONFIG_FILE_PATH = ""
DATA_RETENTION_DAYS = 90
MAX_SLOTS = 16
MAX_SSE_CLIENTS = 50
MAX_PACKETS_IN_MEMORY = 200

# --- Paths ---
STATIC_DIR = ""
LOGIN_HTML_PATH = ""
INDEX_HTML_PATH = ""
NETWORK_HTML_PATH = ""
MAP_HTML_PATH = ""
DMES_HTML_PATH = ""
SETTINGS_HTML_PATH = ""
SENSORS_HTML_PATH = ""
HOOK_HTML_PATH = ""
TASKS_HTML_PATH = ""
PLUGINS_HTML_PATH = ""
PUBLIC_HTML_PATH = ""
FAVICON_PATH = ""
SCRIPT_DIR = ""
PLUGIN_DIR = ""
DATA_DIR = ""
MAPS_DIR = ""
MAPS_CONFIG_FILE = ""
DB_PATH = ""
WEBSERVER_PORT = 8181
DOX_HTML_PATH = ""
COMPARE_HTML_PATH = ""
SHARK_HTML_PATH = ""

# --- Runtime singletons (set after initialization) ---
db_manager = None
meshtastic_data = None
connection_manager = None
NODE_REGISTRY = None
PLUGIN_REGISTRY = None

# --- Plugin runtime (set after plugin loading) ---
_plugin_watchdog = None
_plugin_log_handlers = []
_PLUGIN_LOG_MAX_LINES = 1000

# --- App reference (set after FastAPI app creation) ---
app = None

# --- SSE (set after initialization) ---
sse_queues_lock = None
all_sse_queues_lock = None
all_sse_queues = {}
sse_queues = {}