# Auto-extracted from meshtastic_dashboard.py

import logging
import threading

_PLUGIN_LOG_MAX_LINES = 200
_plugin_log_handlers = {}

class MemoryLogHandler(logging.Handler):
    """Thread-safe circular log buffer attached to a plugin logger (plugin.<pid>)."""
    def __init__(self, maxlen: int = _PLUGIN_LOG_MAX_LINES):
        super().__init__()
        self._buf: list = []
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter(
            '{"ts":"%(asctime)s","lvl":"%(levelname)s","name":"%(name)s","msg":"%(message)s"}',
            datefmt="%Y-%m-%dT%H:%M:%S"
        ))

    def emit(self, record: logging.LogRecord):
        try:
            line = self.format(record)
            with self._lock:
                self._buf.append({"t": record.created, "lvl": record.levelname, "msg": line})
                if len(self._buf) > self._maxlen:
                    self._buf = self._buf[-self._maxlen:]
        except Exception:
            pass

    def get_lines(self) -> list:
        with self._lock:
            return list(self._buf)

    def clear(self):
        with self._lock:
            self._buf.clear()


def _attach_plugin_log_handler(pid: str) -> "MemoryLogHandler":
    """Create and attach a MemoryLogHandler to plugin.<pid> logger. Idempotent."""
    if pid not in _plugin_log_handlers:
        handler = MemoryLogHandler()
        _plugin_log_handlers[pid] = handler
        pl = logging.getLogger(f"plugin.{pid}")
        pl.addHandler(handler)
        pl.setLevel(logging.DEBUG)
    return _plugin_log_handlers[pid]
