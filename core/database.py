# Auto-extracted from meshtastic_dashboard.py
from core.routes.schemas import User
from fastapi import File, status
import core.globals as g
import sqlite3
import time
import threading
import logging

logger = logging.getLogger(__name__)
import json
import asyncio
from typing import Any, Dict, List, Optional

class DatabaseManager:
    def __init__(self, db_path: str, ephemeral: bool = False):
        self.db_path = db_path
        self.ephemeral = ephemeral or (db_path == ":memory:")
        self._local = threading.local()
        # Ephemeral mode: single shared connection protected by a lock.
        # Thread-local connections cannot share :memory: databases across threads.
        self._shared_conn: Optional[sqlite3.Connection] = None
        self._shared_lock = threading.Lock()
        # Write-buffer: accumulates non-critical node updates to batch-commit.
        self._node_writes_pending: int = 0
        self._db_path_hint: str = db_path if not ephemeral else ":memory:"
        self.init_database()

    def _get_connection(self):
        """Return a connection.
        - File mode: thread-local persistent connection (original behaviour).
        - Ephemeral mode: single shared connection serialised by a lock.
          Callers must NOT hold _shared_lock when calling this.
        """
        if self.ephemeral:
            if self._shared_conn is None:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                conn.row_factory = sqlite3.Row
                self._shared_conn = conn
            return self._shared_conn

        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA cache_size = -8000;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA mmap_size = 67108864;")
        self._local.conn = conn
        return conn

    def _execute(self, sql: str, params=()):
        """Thread-safe execute. Uses lock only in ephemeral mode."""
        if self.ephemeral:
            with self._shared_lock:
                conn = self._get_connection()
                return conn.execute(sql, params)
        return self._get_connection().execute(sql, params)

    def _commit(self):
        """Thread-safe commit."""
        if self.ephemeral:
            with self._shared_lock:
                self._get_connection().commit()
        else:
            self._get_connection().commit()

    def init_database(self):
        conn = self._get_connection()
        if not self.ephemeral:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA cache_size = -8000;")
            conn.execute("PRAGMA temp_store = MEMORY;")
            conn.execute("PRAGMA mmap_size = 67108864;")
        c = conn.cursor()

        c.execute("""CREATE TABLE IF NOT EXISTS packets (
            id INTEGER PRIMARY KEY,
            event_id TEXT UNIQUE,
            timestamp REAL,
            rx_time INTEGER,
            from_id TEXT,
            to_id TEXT,
            channel INTEGER,
            portnum TEXT,
            packet_type TEXT,
            rx_snr REAL,
            rx_rssi INTEGER,
            hop_limit INTEGER,
            hop_start INTEGER,
            want_ack BOOLEAN,
            decoded TEXT,
            raw TEXT,
            source TEXT,
            source_confidence REAL DEFAULT 1.0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        
        try:
            c.execute("ALTER TABLE packets ADD COLUMN source TEXT")
            c.execute("ALTER TABLE packets ADD COLUMN source_confidence REAL DEFAULT 1.0")
        except Exception:
            pass

        c.execute("""CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            node_num INTEGER UNIQUE,
            long_name TEXT,
            short_name TEXT,
            macaddr TEXT,
            hw_model TEXT,
            firmware_version TEXT,
            role TEXT,
            is_local BOOLEAN DEFAULT FALSE,
            last_heard INTEGER,
            battery_level INTEGER,
            voltage REAL,
            channel_utilization REAL,
            air_util_tx REAL,
            snr REAL,
            rssi INTEGER,
            latitude REAL,
            longitude REAL,
            altitude INTEGER,
            position_time INTEGER,
            telemetry_time INTEGER,
            user_info TEXT,
            position_info TEXT,
            device_metrics_info TEXT,
            environment_metrics_info TEXT,
            module_config_info TEXT,
            channel_info TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            packet_event_id TEXT UNIQUE,
            mesh_packet_id INTEGER,
            from_id TEXT,
            to_id TEXT,
            channel INTEGER,
            text TEXT,
            timestamp REAL,
            rx_snr REAL,
            rx_rssi INTEGER,
            status TEXT DEFAULT 'DELIVERED',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        try:
            c.execute("ALTER TABLE messages ADD COLUMN mesh_packet_id INTEGER")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE messages ADD COLUMN status TEXT DEFAULT 'DELIVERED'")
        except Exception:
            pass

        c.execute("""CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY,
            node_id TEXT,
            timestamp REAL,
            latitude REAL,
            longitude REAL,
            altitude INTEGER,
            precision_bits INTEGER,
            ground_speed INTEGER,
            ground_track INTEGER,
            sats_in_view INTEGER,
            pdop REAL,
            hdop REAL,
            vdop REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY,
            node_id TEXT,
            timestamp REAL,
            battery_level INTEGER,
            voltage REAL,
            channel_utilization REAL,
            air_util_tx REAL,
            uptime_seconds INTEGER,
            temperature REAL,
            relative_humidity REAL,
            barometric_pressure REAL,
            gas_resistance REAL,
            iaq REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS average_metrics_history (
            id INTEGER PRIMARY KEY,
            timestamp REAL UNIQUE,
            average_snr REAL,
            average_rssi REAL,
            node_count INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            hashed_password TEXT,
            disabled BOOLEAN DEFAULT FALSE,
            role INTEGER DEFAULT 1,
            force_mfa BOOLEAN DEFAULT FALSE,
            must_setup_mfa BOOLEAN DEFAULT FALSE,
            totp_secret TEXT DEFAULT NULL,
            totp_enabled BOOLEAN DEFAULT FALSE,
            backup_codes TEXT DEFAULT NULL,
            last_login DATETIME DEFAULT NULL,
            login_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        # --- Migration: add columns to existing users tables ---
        _user_migrations = [
            ("totp_secret", "TEXT DEFAULT NULL"),
            ("totp_enabled", "BOOLEAN DEFAULT FALSE"),
            ("backup_codes", "TEXT DEFAULT NULL"),
            ("role", "INTEGER DEFAULT 1"),
            ("force_mfa", "BOOLEAN DEFAULT FALSE"),
            ("must_setup_mfa", "BOOLEAN DEFAULT FALSE"),
            ("last_login", "DATETIME DEFAULT NULL"),
            ("login_count", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_def in _user_migrations:
            try:
                c.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # Ensure the very first user (lowest id) is always admin (role=0)
        # This covers existing installs where the column was just added with DEFAULT 1
        try:
            c.execute("UPDATE users SET role = 0 WHERE id = (SELECT MIN(id) FROM users) AND role != 0")
        except Exception:
            pass

        c.execute("""CREATE TABLE IF NOT EXISTS neighbors (
            node_id TEXT,
            neighbor_id TEXT,
            snr REAL,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (node_id, neighbor_id)
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS traceroutes (
            id INTEGER PRIMARY KEY,
            from_id TEXT,
            to_id TEXT,
            route_path TEXT,
            timestamp REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS waypoints (
            id INTEGER PRIMARY KEY,
            from_id TEXT,
            waypoint_id INTEGER,
            name TEXT,
            latitude REAL,
            longitude REAL,
            description TEXT,
            timestamp REAL,
            UNIQUE(from_id, waypoint_id)
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS hardware_logs (
            id INTEGER PRIMARY KEY,
            node_id TEXT,
            event_type TEXT,
            details TEXT,
            timestamp REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS connection_log (
            id INTEGER PRIMARY KEY,
            timestamp REAL,
            status TEXT,
            value REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        indices = [
            ("idx_packets_ts", "packets", "timestamp"),
            ("idx_packets_from_id", "packets", "from_id"),
            ("idx_packets_from_ts", "packets", "from_id, timestamp DESC"),
            ("idx_messages_ts", "messages", "timestamp"),
            ("idx_messages_from_id", "messages", "from_id"),
            ("idx_messages_to_id", "messages", "to_id"),                          # MQTT observer queries
            ("idx_messages_channel_ts", "messages", "channel, timestamp DESC"),
            ("idx_messages_conversation", "messages", "from_id, to_id"),
            ("idx_messages_from_to_ch_ts", "messages", "from_id, to_id, channel, timestamp DESC"),
            ("idx_messages_status_ts", "messages", "status, timestamp DESC"),     # ACK update queries
            ("idx_positions_ts", "positions", "timestamp"),
            ("idx_positions_node_ts", "positions", "node_id, timestamp DESC"),
            ("idx_positions_node_id", "positions", "node_id"),               # COUNT(*) covering
            ("idx_telemetry_ts", "telemetry", "timestamp"),
            ("idx_telemetry_node_ts", "telemetry", "node_id, timestamp DESC"),
            ("idx_telemetry_node_id", "telemetry", "node_id"),               # COUNT(*) covering
            ("idx_hw_logs_ts", "hardware_logs", "timestamp"),
            ("idx_hw_logs_node_ts", "hardware_logs", "node_id, timestamp DESC"),
            ("idx_conn_log_ts", "connection_log", "timestamp"),
            ("idx_nodes_last_heard", "nodes", "last_heard"),
            ("idx_nodes_is_local", "nodes", "is_local"),
            ("idx_traceroutes_ts", "traceroutes", "timestamp DESC"),
            ("idx_avg_metrics_ts", "average_metrics_history", "timestamp DESC"),
        ]
        for name, table, cols in indices:
            try:
                c.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})")
            except Exception as e:
                logging.warning(f"? Could not create index {name}: {e}")
        conn.commit()

    # --- User management ---

    def get_user(self, username: str) -> Optional[Dict]:
        conn = self._get_connection()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def count_users(self) -> int:
        conn = self._get_connection()
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def atomic_setup_user(self, username: str, hashed_password: str) -> Optional[Dict]:
        """Atomically check user count AND insert the first admin user.

        Uses BEGIN IMMEDIATE to serialise concurrent setup attempts.
        Returns the user dict on success, None if setup was already done.
        Safe to call from multiple threads."""
        if self.ephemeral:
            with self._shared_lock:
                return self._atomic_setup_user_impl(username, hashed_password)
        return self._atomic_setup_user_impl(username, hashed_password)

    def _atomic_setup_user_impl(self, username: str, hashed_password: str) -> Optional[Dict]:
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count > 0:
                conn.execute("ROLLBACK")
                return None
            cur = conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES (?, ?, 0)",
                (username, hashed_password),
            )
            conn.commit()
            return {"id": cur.lastrowid}
        except sqlite3.IntegrityError:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return None
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def create_user(self, username: str, hashed_password: str, role: int = 1,
                    force_mfa: bool = False, must_setup_mfa: bool = False) -> Optional[Dict]:
        try:
            conn = self._get_connection()
            cur = conn.execute(
                "INSERT INTO users (username, hashed_password, role, force_mfa, must_setup_mfa) VALUES (?, ?, ?, ?, ?)",
                (username, hashed_password, role, force_mfa, must_setup_mfa),
            )
            conn.commit()
            return {"id": cur.lastrowid}
        except sqlite3.IntegrityError:
            return None

    def get_all_users(self) -> List[Dict]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT id, username, disabled, role, totp_enabled, force_mfa, must_setup_mfa, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_user_role(self, username: str, role: int):
        conn = self._get_connection()
        conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
        conn.commit()

    def update_user_password(self, username: str, hashed_password: str):
        conn = self._get_connection()
        conn.execute("UPDATE users SET hashed_password = ? WHERE username = ?", (hashed_password, username))
        conn.commit()

    def suspend_user(self, username: str, suspended: bool):
        conn = self._get_connection()
        conn.execute("UPDATE users SET disabled = ? WHERE username = ?", (suspended, username))
        conn.commit()

    def delete_user(self, username: str) -> bool:
        conn = self._get_connection()
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        return cur.rowcount > 0

    def set_force_mfa(self, username: str, force: bool, must_setup: bool = False):
        conn = self._get_connection()
        conn.execute(
            "UPDATE users SET force_mfa = ?, must_setup_mfa = ? WHERE username = ?",
            (force, must_setup, username),
        )
        conn.commit()

    def clear_must_setup_mfa(self, username: str):
        conn = self._get_connection()
        conn.execute("UPDATE users SET must_setup_mfa = FALSE WHERE username = ?", (username,))
        conn.commit()

    def record_login(self, username: str):
        conn = self._get_connection()
        conn.execute(
            "UPDATE users SET last_login = CURRENT_TIMESTAMP, login_count = COALESCE(login_count, 0) + 1 WHERE username = ?",
            (username,),
        )
        conn.commit()

    def get_all_users_full(self) -> List[Dict]:
        """Return all users with login stats for admin dashboard."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT id, username, disabled, role, totp_enabled, force_mfa, must_setup_mfa, "
            "last_login, login_count, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- TOTP / MFA management ---

    def set_totp_secret(self, username: str, secret: str):
        conn = self._get_connection()
        conn.execute("UPDATE users SET totp_secret = ? WHERE username = ?", (secret, username))
        conn.commit()

    def enable_totp(self, username: str, backup_codes_json: str):
        conn = self._get_connection()
        conn.execute(
            "UPDATE users SET totp_enabled = TRUE, backup_codes = ? WHERE username = ?",
            (backup_codes_json, username),
        )
        conn.commit()

    def disable_totp(self, username: str):
        conn = self._get_connection()
        conn.execute(
            "UPDATE users SET totp_enabled = FALSE, totp_secret = NULL, backup_codes = NULL WHERE username = ?",
            (username,),
        )
        conn.commit()

    def consume_backup_code(self, username: str, updated_codes_json: str):
        conn = self._get_connection()
        conn.execute("UPDATE users SET backup_codes = ? WHERE username = ?", (updated_codes_json, username))
        conn.commit()

    # --- Packet / message / telemetry saving ---

    def save_packet(self, packet: Dict):
        """Persist a packet and its derived sub-records in one transaction.

        All INSERTs (packets, messages/positions/telemetry, ACK updates) are
        batched inside a single BEGIN/COMMIT instead of committing after each
        sub-statement.  On a busy mesh this halves WAL I/O without sacrificing
        durability  WAL mode means readers are never blocked regardless.
        """
        try:
            conn = self._get_connection()
            decoded = json.dumps(packet.get("decoded")) if packet.get("decoded") else None
            raw     = json.dumps(packet.get("raw"))     if packet.get("raw")     else None

            with conn:  # single transaction: commits on success, rolls back on error
                conn.execute(
                    """INSERT OR REPLACE INTO packets
                    (event_id, timestamp, rx_time, from_id, to_id, channel, packet_type,
                     rx_snr, rx_rssi, hop_limit, hop_start, want_ack, decoded, raw, source, source_confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        packet.get("event_id"),
                        packet.get("timestamp"),
                        packet.get("rxTime"),
                        packet.get("fromId"),
                        packet.get("toId"),
                        packet.get("channel"),
                        packet.get("app_packet_type"),
                        packet.get("rxSnr"),
                        packet.get("rxRssi"),
                        packet.get("hopLimit"),
                        packet.get("hopStart"),
                        packet.get("wantAck"),
                        decoded,
                        raw,
                        packet.get("source"),
                        packet.get("source_confidence", 1.0),
                    ),
                )

                ptype   = packet.get("app_packet_type")
                from_id = packet.get("fromId")

                if ptype == "Message" and from_id:
                    decoded_obj = packet.get("decoded", {})
                    pl = None
                    for key in ["payload", "text", "string", "message"]:
                        if key in decoded_obj:
                            pl = decoded_obj[key]
                            break
                    if isinstance(pl, bytes):
                        try:
                            pl = pl.decode("utf-8")
                        except Exception:
                            pl = None
                    if isinstance(pl, str) and pl:
                        mesh_pkt_id = packet.get("id") or decoded_obj.get("mesh_packet_id")
                        status_val = "BROADCAST" if str(packet.get("toId")) == "^all" else "DELIVERED"
                        conn.execute(
                            """INSERT INTO messages
                            (packet_event_id, mesh_packet_id, from_id, to_id, channel, text, timestamp, rx_snr, rx_rssi, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(packet_event_id) DO NOTHING""",
                            (
                                packet["event_id"],
                                mesh_pkt_id,
                                from_id,
                                packet.get("toId"),
                                packet.get("channel"),
                                pl,
                                packet["timestamp"],
                                packet.get("rxSnr"),
                                packet.get("rxRssi"),
                                status_val,
                            ),
                        )

                elif ptype == "Encrypted" and from_id:
                    mesh_pkt_id = packet.get("id")
                    to_id_val   = packet.get("toId") or "^all"
                    status_val  = "BROADCAST" if str(to_id_val) == "^all" else "ENCRYPTED"
                    conn.execute(
                        """INSERT INTO messages
                        (packet_event_id, mesh_packet_id, from_id, to_id, channel, text, timestamp, rx_snr, rx_rssi, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(packet_event_id) DO NOTHING""",
                        (
                            packet["event_id"],
                            mesh_pkt_id,
                            from_id,
                            to_id_val,
                            packet.get("channel"),
                            None,
                            packet["timestamp"],
                            packet.get("rxSnr"),
                            packet.get("rxRssi"),
                            status_val,
                        ),
                    )

                elif ptype in ["Ack", "Routing Error"]:
                    decoded_obj = packet.get("decoded", {})
                    req_id = decoded_obj.get("requestId")
                    if req_id:
                        status_val = "DELIVERED" if ptype == "Ack" else "FAILED"
                        conn.execute(
                            "UPDATE messages SET status = ? WHERE mesh_packet_id = ?",
                            (status_val, req_id)
                        )
                        # SSE broadcast happens outside this transaction  no deadlock risk
                        if g.main_event_loop:
                            asyncio.run_coroutine_threadsafe(
                                broadcast_data({
                                    "event": "message_status_update",
                                    "data": {"mesh_packet_id": req_id, "status": status_val}
                                }),
                                g.main_event_loop
                            )

                elif (ptype == "Position" or "position" in packet.get("decoded", {})) and from_id:
                    pos = packet.get("decoded", {}).get("position", {})
                    if pos:
                        lat = pos.get("latitude")
                        if lat is None and pos.get("latitudeI"):
                            lat = pos["latitudeI"] * 1e-7
                        lon = pos.get("longitude")
                        if lon is None and pos.get("longitudeI"):
                            lon = pos["longitudeI"] * 1e-7
                        if lat is not None and lon is not None:
                            conn.execute(
                                """INSERT INTO positions
                                (node_id, timestamp, latitude, longitude, altitude, precision_bits,
                                 ground_speed, ground_track, sats_in_view, pdop, hdop, vdop)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    from_id,
                                    packet["timestamp"],
                                    lat, lon,
                                    pos.get("altitude"),
                                    pos.get("precisionBits"),
                                    pos.get("groundSpeed"),
                                    pos.get("groundTrack"),
                                    pos.get("satsInView"),
                                    pos.get("PDOP"),
                                    pos.get("HDOP"),
                                    pos.get("VDOP"),
                                ),
                            )

                elif (ptype == "Telemetry" or "telemetry" in packet.get("decoded", {})) and from_id:
                    tel = packet.get("decoded", {}).get("telemetry", {})
                    if tel:
                        d = tel.get("deviceMetrics", {})
                        e = tel.get("environmentMetrics", {})
                        if d or e:
                            conn.execute(
                                """INSERT INTO telemetry
                                (node_id, timestamp, battery_level, voltage, channel_utilization,
                                 air_util_tx, uptime_seconds, temperature, relative_humidity,
                                 barometric_pressure, gas_resistance, iaq)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    from_id,
                                    packet["timestamp"],
                                    d.get("batteryLevel"),
                                    d.get("voltage"),
                                    d.get("channelUtilization"),
                                    d.get("airUtilTx"),
                                    d.get("uptimeSeconds"),
                                    e.get("temperature"),
                                    e.get("relativeHumidity"),
                                    e.get("barometricPressure"),
                                    e.get("gasResistance"),
                                    e.get("iaq"),
                                ),
                            )
        except Exception as e:
            logger.error(f"DB Error save_packet: {e}")

    def save_node(self, node_id: str, data: Dict):
        """Write node to DB.

        Hot-path optimisation: instead of committing every call (which fires
        on every packet received), we buffer dirty nodes and flush in a single
        batch transaction.  _flush_node_write_buffer() is called by the
        background maintenance worker every NODE_WRITE_FLUSH_INTERVAL seconds.

        We do commit immediately when a *meaningful* field changes (long_name,
        position with coordinates, firmware_version) because those are rare
        and the user expects them to survive a restart quickly.
        """
        # Determine whether this update contains a field worth a hard commit.
        immediate_commit = (
            data.get("user", {}).get("longName") is not None
            or (data.get("position", {}).get("latitude") is not None)
            or data.get("firmware_version") is not None
            or data.get("hw_model") is not None
        )

        try:
            conn = self._get_connection()
            user        = json.dumps(data.get("user", {}))
            pos         = json.dumps(data.get("position", {}))
            metrics     = json.dumps(data.get("deviceMetrics", {}))
            env_metrics = json.dumps(data.get("environmentMetrics", {}))
            mod_conf    = json.dumps(data.get("moduleConfig", {}))
            chan_info   = json.dumps(data.get("channelSettings", {}))

            num = data.get("num")
            if num is None and node_id.startswith("!"):
                try:
                    num = int(node_id[1:], 16)
                except Exception:
                    pass

            lat = data.get("latitude")
            lon = data.get("longitude")
            if lat is None:
                lat_i = data.get("position", {}).get("latitudeI")
                if lat_i:
                    lat = lat_i / 1e7
            if lon is None:
                lon_i = data.get("position", {}).get("longitudeI")
                if lon_i:
                    lon = lon_i / 1e7

            conn.execute(
                """INSERT INTO nodes
                (node_id, node_num, long_name, short_name, macaddr, hw_model, firmware_version,
                 role, is_local, last_heard, battery_level, voltage, channel_utilization,
                 air_util_tx, snr, rssi, latitude, longitude, altitude, position_time,
                 telemetry_time, user_info, position_info, device_metrics_info,
                 environment_metrics_info, module_config_info, channel_info, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(node_id) DO UPDATE SET
                node_num=excluded.node_num,
                long_name=COALESCE(excluded.long_name, nodes.long_name),
                short_name=COALESCE(excluded.short_name, nodes.short_name),
                hw_model=COALESCE(excluded.hw_model, nodes.hw_model),
                last_heard=MAX(excluded.last_heard, nodes.last_heard),
                battery_level=COALESCE(excluded.battery_level, nodes.battery_level),
                voltage=COALESCE(excluded.voltage, nodes.voltage),
                snr=excluded.snr, rssi=excluded.rssi,
                latitude=COALESCE(excluded.latitude, nodes.latitude),
                longitude=COALESCE(excluded.longitude, nodes.longitude),
                user_info=CASE WHEN excluded.user_info != '{}' THEN excluded.user_info ELSE nodes.user_info END,
                position_info=CASE WHEN excluded.position_info != '{}' THEN excluded.position_info ELSE nodes.position_info END,
                device_metrics_info=CASE WHEN excluded.device_metrics_info != '{}' THEN excluded.device_metrics_info ELSE nodes.device_metrics_info END,
                environment_metrics_info=CASE WHEN excluded.environment_metrics_info != '{}' THEN excluded.environment_metrics_info ELSE nodes.environment_metrics_info END,
                updated_at=CURRENT_TIMESTAMP""",
                (
                    node_id, num,
                    data.get("user", {}).get("longName"),
                    data.get("user", {}).get("shortName"),
                    data.get("user", {}).get("macaddr"),
                    data.get("hw_model"),
                    data.get("firmware_version"),
                    str(data.get("role")),
                    data.get("isLocal"),
                    data.get("lastHeard"),
                    data.get("battery_level"),
                    data.get("voltage"),
                    data.get("channel_utilization"),
                    data.get("air_util_tx"),
                    data.get("snr"),
                    data.get("rssi"),
                    lat, lon,
                    data.get("altitude"),
                    data.get("position_time"),
                    data.get("telemetry_time"),
                    user, pos, metrics, env_metrics, mod_conf, chan_info,
                ),
            )

            conn.commit()

        except Exception as e:
            logger.error(f"DB Error save_node: {e}")


    def flush_node_write_buffer(self):
        """Commit any pending (non-immediate) node writes to disk.

        Called by the background maintenance worker every
        NODE_WRITE_FLUSH_INTERVAL seconds, and also on clean shutdown.
        Thread-safe: always called via asyncio.to_thread().
        """
        if self._node_writes_pending == 0:
            return
        try:
            conn = self._get_connection()
            conn.commit()
            logger.debug(
                "DB: flushed %d buffered node write(s) for %s",
                self._node_writes_pending, getattr(self, '_db_path_hint', ''),
            )
        except Exception as e:
            logger.error("DB Error flush_node_write_buffer: %s", e)
        finally:
            self._node_writes_pending = 0

    def get_all_nodes(self) -> Dict[str, Dict]:
        """Load all nodes from DB for initial in-memory population.

        Only fetches the columns the frontend actually uses for display
        (identity, position, telemetry, last_heard).  The heavyweight blobs
        module_config_info and channel_info are omitted  they are only
        needed when a user explicitly opens Node Config and are fetched
        on-demand from there.  This makes startup and slot-switch significantly
        faster for large node databases.
        """
        nodes = {}
        conn = self._get_connection()
        rows = conn.execute("""
            SELECT
                node_id, node_num, long_name, short_name, macaddr,
                hw_model, firmware_version, role, is_local,
                last_heard, battery_level, voltage,
                channel_utilization, air_util_tx, snr, rssi,
                latitude, longitude, altitude,
                position_time, telemetry_time,
                user_info, position_info,
                device_metrics_info, environment_metrics_info
            FROM nodes
        """).fetchall()

        slim_json_fields = [
            "user_info", "position_info",
            "device_metrics_info", "environment_metrics_info",
        ]
        rename_map = {
            "user_info":                "user",
            "position_info":            "position",
            "device_metrics_info":      "deviceMetrics",
            "environment_metrics_info": "environmentMetrics",
        }

        for row in rows:
            d = dict(row)

            for f in slim_json_fields:
                try:
                    d[f] = json.loads(d[f]) if d[f] else {}
                except Exception:
                    d[f] = {}
            for old_key, new_key in rename_map.items():
                d[new_key] = d.pop(old_key)

            # Normalise snake_case  camelCase for frontend compatibility
            lh = d.pop("last_heard", None)
            if lh is not None:
                d.setdefault("lastHeard", lh)

            il = d.pop("is_local", None)
            if il is not None:
                d.setdefault("isLocal", bool(il))

            nodes[d["node_id"]] = d
        return nodes

    def get_recent_packets(self, limit: int = 100) -> List[Dict]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM packets ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            if d.get("decoded"):
                try:
                    d["decoded"] = json.loads(d["decoded"])
                except Exception:
                    pass
            if d.get("raw"):
                try:
                    d["raw"] = json.loads(d["raw"])
                except Exception:
                    pass
            res.append(d)
        return res

    def get_messages(
        self,
        from_id=None,
        to_id=None,
        channel=None,
        start=None,
        end=None,
        limit=100,
    ) -> List[Dict]:
        # Select only the columns the frontend actually consumes.
        # Omitting created_at saves a TEXT parse per row; this is called
        # frequently (DMs poll every 5s, channels poll every 5s).
        q = """SELECT
                   packet_event_id, mesh_packet_id,
                   from_id, to_id, channel,
                   text, timestamp, rx_snr, rx_rssi, status
               FROM messages WHERE 1=1"""
        p: list = []
        if from_id:
            q += " AND from_id = ?"
            p.append(from_id)
        if to_id:
            q += " AND to_id = ?"
            p.append(to_id)
        if channel is not None:
            if not to_id or to_id == "^all":
                q += " AND channel = ?"
                p.append(channel)
        if start:
            q += " AND timestamp >= ?"
            p.append(start)
        if end:
            q += " AND timestamp <= ?"
            p.append(end)
        q += " ORDER BY timestamp DESC LIMIT ?"
        p.append(limit)
        conn = self._get_connection()
        rows = conn.execute(q, p).fetchall()
        return [dict(r) for r in rows]

    def count_node_items(self, node_id, item_type, start=None, end=None) -> int:
        table = {"messages_sent": "messages", "positions": "positions", "telemetry": "telemetry"}.get(item_type)
        if not table:
            return -1
        col = "from_id" if item_type == "messages_sent" else "node_id"
        q = f"SELECT COUNT(*) FROM {table} WHERE 1=1"
        p = []
        if node_id:
            q += f" AND {col} = ?"
            p.append(node_id)
        if start:
            q += " AND timestamp >= ?"
            p.append(start)
        if end:
            q += " AND timestamp <= ?"
            p.append(end)
        conn = self._get_connection()
        return conn.execute(q, p).fetchone()[0]

    def calculate_and_save_average_metrics(self):
        conn = self._get_connection()
        row = conn.execute(
            "SELECT ROUND(AVG(snr), 2) as avg_snr, ROUND(AVG(rssi), 1) as avg_rssi, COUNT(*) as cnt "
            "FROM nodes WHERE snr IS NOT NULL AND rssi IS NOT NULL AND is_local = FALSE"
        ).fetchone()
        if not row or row["cnt"] == 0:
            return
        conn.execute(
            "INSERT INTO average_metrics_history (timestamp, average_snr, average_rssi, node_count) VALUES (?, ?, ?, ?)",
            (time.time(), row["avg_snr"], row["avg_rssi"], row["cnt"]),
        )
        conn.commit()

    def get_most_recent_average_metrics(self) -> Optional[Dict]:
        conn = self._get_connection()
        r = conn.execute(
            "SELECT * FROM average_metrics_history ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else None

    def get_average_metrics_history(self, limit=100) -> List[Dict]:
        conn = self._get_connection()
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM average_metrics_history ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def prune_old_data(self, max_age_days: int):
        """Deletes old data in batches to avoid long locks, then checkpoints WAL."""
        cutoff = time.time() - (max_age_days * 86400)
        logger.info(f"? Database Maintenance: Pruning data older than {max_age_days * 24} hours...")
        try:
            conn = self._get_connection()
            total_deleted = 0
            for table in ["average_metrics_history", "packets", "messages", "telemetry",
                           "positions", "connection_log"]:
                while True:
                    cursor = conn.execute(
                        f"DELETE FROM {table} WHERE rowid IN "
                        f"(SELECT rowid FROM {table} WHERE timestamp < ? LIMIT 5000)",
                        (cutoff,)
                    )
                    batch = cursor.rowcount
                    conn.commit()
                    total_deleted += batch
                    if batch < 5000:
                        break
            if not self.ephemeral:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            logger.info(f" Database maintenance complete. Deleted {total_deleted} old rows.")
        except Exception as e:
            logger.error(f" Database maintenance failed: {e}")

    def log_connection_status(self, status_str: str):
        sl = status_str.lower()
        val = (
            0.9 if ("connected" in sl or "web serial" in sl)
            else (0.5 if ("init" in sl or "wait" in sl or "reconnect" in sl or "degraded" in sl or "stream open" in sl)
            else 0.1)
        )
        try:
            conn = self._get_connection()
            conn.execute(
                "INSERT INTO connection_log (timestamp, status, value) VALUES (?, ?, ?)",
                (time.time(), status_str, val),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"DB Error log_connection_status: {e}")

    def get_connection_history(self, limit=100):
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT timestamp, value, status FROM connection_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_node_history(self, node_id, table, start=None, end=None, limit=1000):
        if table not in ["positions", "telemetry"]:
            return []

        # For the positions table only fetch the columns the map and panel
        # actually consume.  pdop/hdop/vdop/precision_bits are never read by
        # the frontend; omitting them roughly halves position row payload size.
        if table == "positions":
            cols = "node_id, timestamp, latitude, longitude, altitude, ground_speed, ground_track, sats_in_view"
        else:
            cols = "*"

        q = f"SELECT {cols} FROM {table} WHERE node_id = ?"
        p: list = [node_id]
        if start:
            q += " AND timestamp >= ?"
            p.append(start)
        if end:
            q += " AND timestamp <= ?"
            p.append(end)
        q += " ORDER BY timestamp DESC LIMIT ?"
        p.append(limit)
        conn = self._get_connection()
        return [dict(r) for r in conn.execute(q, p).fetchall()]

    def save_neighbors(self, node_id: str, neighbors_list: list):
        try:
            conn = self._get_connection()
            for n in neighbors_list:
                n_id_int = n.get("nodeId")
                if not n_id_int:
                    continue
                neighbor_hex = f"!{n_id_int:08x}"
                conn.execute(
                    """INSERT OR REPLACE INTO neighbors
                    (node_id, neighbor_id, snr, last_seen)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
                    (node_id, neighbor_hex, n.get("snr")),
                )
            conn.commit()
        except Exception as e:
            logging.error(f"DB Error save_neighbors: {e}")

    def save_traceroute(self, from_id: str, to_id: str, route_list: list, timestamp: float,
                        route_back: list = None, snr_towards: list = None, snr_back: list = None,
                        rssi: int = None, snr: float = None, hops_used: int = None):
        try:
            def hex_ids(lst):
                result = []
                for r in (lst or []):
                    if isinstance(r, int):
                        result.append(f"!{r:08x}")
                    else:
                        result.append(str(r))
                return result

            payload = {
                "route_to":     hex_ids(route_list),
                "route_back":   hex_ids(route_back or []),
                "snr_towards":  [v / 4.0 for v in (snr_towards or [])],
                "snr_back":     [v / 4.0 for v in (snr_back or [])],
                "rssi":         rssi,
                "snr":          snr,
                "hops_used":    hops_used,
            }
            conn = self._get_connection()
            conn.execute(
                "INSERT INTO traceroutes (from_id, to_id, route_path, timestamp) VALUES (?, ?, ?, ?)",
                (from_id, to_id, json.dumps(payload), timestamp),
            )
            conn.commit()
        except Exception as e:
            logging.error(f"DB Error save_traceroute: {e}")

    def save_waypoint(self, from_id: str, wp: dict, timestamp: float):
        try:
            conn = self._get_connection()
            conn.execute(
                """INSERT OR REPLACE INTO waypoints
                (from_id, waypoint_id, name, latitude, longitude, description, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    from_id,
                    wp.get("id", 0),
                    wp.get("name"),
                    wp.get("latitude", 0) / 1e7 if wp.get("latitude") else 0,
                    wp.get("longitude", 0) / 1e7 if wp.get("longitude") else 0,
                    wp.get("description"),
                    timestamp,
                ),
            )
            conn.commit()
        except Exception as e:
            logging.error(f"DB Error save_waypoint: {e}")

    def log_hardware_event(self, node_id: str, event_type: str, details: Any, timestamp: float):
        try:
            conn = self._get_connection()
            conn.execute(
                "INSERT INTO hardware_logs (node_id, event_type, details, timestamp) VALUES (?, ?, ?, ?)",
                (
                    node_id,
                    event_type,
                    json.dumps(details) if isinstance(details, (dict, list)) else str(details),
                    timestamp,
                ),
            )
            conn.commit()
        except Exception as e:
            logging.error(f"DB Error log_hardware_event: {e}")

    def get_neighbors(self, limit=500):
        conn = self._get_connection()
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM neighbors ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        ]

    def get_traceroutes(self, limit=100):
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM traceroutes ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            try:
                d["route_path"] = json.loads(d["route_path"])
            except Exception:
                pass
            res.append(d)
        return res

    def get_waypoints(self, limit=500):
        conn = self._get_connection()
        return [dict(r) for r in conn.execute("SELECT * FROM waypoints ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()]

    def get_hardware_logs(self, limit=100):
        conn = self._get_connection()
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM hardware_logs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def global_search(self, query: str, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        q_like = f"%{query}%"
        results = []
        try:
            # Comprehensive Node Search (Checks names, IDs, MAC, and raw user info json)
            n_rows = conn.execute(
                "SELECT 'Node' as type, node_id as target_id, COALESCE(long_name, node_id) as title, "
                "COALESCE(hw_model, 'Unknown HW') || ' (' || COALESCE(role, 'CLIENT') || ')' as snippet FROM nodes "
                "WHERE long_name LIKE ? OR short_name LIKE ? OR node_id LIKE ? OR macaddr LIKE ? OR user_info LIKE ? LIMIT ?",
                (q_like, q_like, q_like, q_like, q_like, limit)
            ).fetchall()
            for r in n_rows:
                d = dict(r)
                d["icon"] = "fas fa-microchip"
                d["action"] = f"window.c2OpenNodeDetail('{d['target_id']}')"
                results.append(d)

            # Comprehensive Message Search (Checks message bodies)
            m_rows = conn.execute(
                "SELECT 'Message' as type, from_id as target_id, 'Message from ' || from_id as title, "
                "text as snippet FROM messages WHERE text LIKE ? LIMIT ?",
                (q_like, limit)
            ).fetchall()
            for r in m_rows:
                d = dict(r)
                d["icon"] = "fas fa-envelope"
                # Opens node detail modal and jumps straight to the messages tab for that conversation
                d["action"] = f"window.c2OpenNodeDetail('{d['target_id']}'); setTimeout(()=>window.c2SwitchModalTab('messages', '{d['target_id']}'), 100);"
                results.append(d)

            # Comprehensive Waypoint Search (Checks waypoint names and descriptions)
            w_rows = conn.execute(
                "SELECT 'Waypoint' as type, from_id as target_id, name as title, "
                "description as snippet FROM waypoints WHERE name LIKE ? OR description LIKE ? LIMIT ?",
                (q_like, q_like, limit)
            ).fetchall()
            for r in w_rows:
                d = dict(r)
                d["icon"] = "fas fa-map-marker-alt"
                d["action"] = f"window.c2OpenNodeDetail('{d['target_id']}')"
                results.append(d)

        except Exception as e:
            logging.error(f"Global search DB error: {e}")
        
        return results


