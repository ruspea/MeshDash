# Auto-extracted from meshtastic_dashboard.py

import base64
import socket
import struct as _struct
from google.protobuf.descriptor import FieldDescriptor as _FD

_NC_NUMERIC_TYPES = (
    _FD.TYPE_INT32, _FD.TYPE_INT64, _FD.TYPE_UINT32, _FD.TYPE_UINT64,
    _FD.TYPE_SINT32, _FD.TYPE_SINT64, _FD.TYPE_FIXED32, _FD.TYPE_FIXED64,
    _FD.TYPE_SFIXED32, _FD.TYPE_SFIXED64, _FD.TYPE_FLOAT, _FD.TYPE_DOUBLE
)
_NC_IP_FIELDS = {'ip', 'gateway', 'subnet', 'dns'}

def _nc_int_to_ip(n: int) -> str:
    try:
        return socket.inet_ntoa(_struct.pack("<I", n))
    except Exception:
        return "0.0.0.0"


def _nc_ip_to_int(s: str) -> int:
    try:
        return _struct.unpack("<I", socket.inet_aton(s))[0]
    except Exception:
        return 0


def _nc_flatten_message(obj, path: str, out: list):
    for field in obj.DESCRIPTOR.fields:
        val = getattr(obj, field.name)
        fpath = f"{path}.{field.name}"
        
        is_repeated = getattr(field, 'label', 0) == 3 or "Repeated" in type(val).__name__
        if is_repeated:
            out.append({"path": fpath, "name": field.name, "type": "repeated", "value": str(val), "readonly": True})
            continue
        if field.type == _FD.TYPE_MESSAGE:
            _nc_flatten_message(val, fpath, out)
        elif field.type == _FD.TYPE_ENUM:
            options = {ev.name: ev.number for ev in field.enum_type.values}
            out.append({"path": fpath, "name": field.name, "type": "enum", "value": val, "options": options})
        elif field.type == _FD.TYPE_BOOL:
            out.append({"path": fpath, "name": field.name, "type": "bool", "value": val})
        elif field.type in _NC_NUMERIC_TYPES and field.name in _NC_IP_FIELDS:
            out.append({"path": fpath, "name": field.name, "type": "ip", "value": _nc_int_to_ip(val)})
        elif field.type in (_FD.TYPE_FLOAT, _FD.TYPE_DOUBLE):
            out.append({"path": fpath, "name": field.name, "type": "float", "value": val})
        elif field.type in _NC_NUMERIC_TYPES:
            out.append({"path": fpath, "name": field.name, "type": "int", "value": val})
        elif field.type == _FD.TYPE_BYTES:
            out.append({"path": fpath, "name": field.name, "type": "bytes", "value": base64.b64encode(val).decode() if val else ""})
        else:
            out.append({"path": fpath, "name": field.name, "type": "string", "value": str(val) if val is not None else ""})


def _nc_coerce(field: _FD, value: str, prop_name: str):
    if field.type == _FD.TYPE_BOOL:
        return str(value).lower() in ('true', '1', 'on', 'yes')
    if field.type in _NC_NUMERIC_TYPES and prop_name in _NC_IP_FIELDS:
        return _nc_ip_to_int(value)
    if field.type in (_FD.TYPE_FLOAT, _FD.TYPE_DOUBLE):
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0
    if field.type in _NC_NUMERIC_TYPES:
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return 0
    if field.type == _FD.TYPE_ENUM:
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0
    if field.type == _FD.TYPE_BYTES:
        if isinstance(value, str):
            try:
                return base64.b64decode(value)
            except Exception:
                return b''
        return value if isinstance(value, bytes) else b''
    return str(value)


def _nc_build_snapshot(iface) -> dict:
    node = iface.localNode
    user_data = iface.getMyUser() or {}
    snapshot = {
        "identity": {
            "long_name": user_data.get("longName", ""),
            "short_name": user_data.get("shortName", ""),
        },
        "localConfig": {},
        "moduleConfig": {},
        "channels": {},
    }
    for sf in node.localConfig.DESCRIPTOR.fields:
        if sf.type == _FD.TYPE_MESSAGE:
            fields = []
            _nc_flatten_message(getattr(node.localConfig, sf.name), f"localConfig.{sf.name}", fields)
            snapshot["localConfig"][sf.name] = fields
    for sf in node.moduleConfig.DESCRIPTOR.fields:
        if sf.type == _FD.TYPE_MESSAGE:
            fields = []
            _nc_flatten_message(getattr(node.moduleConfig, sf.name), f"moduleConfig.{sf.name}", fields)
            snapshot["moduleConfig"][sf.name] = fields
    for ch in node.channels:
        if ch.role != 0:
            fields = []
            _nc_flatten_message(ch.settings, f"channels.{ch.index}", fields)
            snapshot["channels"][str(ch.index)] = {
                "index": ch.index,
                "role": str(ch.role),
                "fields": fields,
            }
    return snapshot


def _nc_apply_changes(iface, changes: list) -> dict:
    node = iface.localNode
    sections_local: set = set()
    sections_module: set = set()
    channels_written: set = set()
    errors: list = []
    identity_change = {}
    for item in changes:
        key: str = item.get("path", "")
        value = item.get("value")
        if not key or key.startswith("IGNORE_"):
            continue
        if key.startswith("identity."):
            prop = key.split(".", 1)[1]
            identity_change[prop] = value
            continue
        parts = key.split(".")
        root = parts[0]
        try:
            if root in ("localConfig", "moduleConfig"):
                section = parts[1]
                target_obj = getattr(node, root)
                for part in parts[1:-1]:
                    target_obj = getattr(target_obj, part)
                prop_name = parts[-1]
                field_desc = target_obj.DESCRIPTOR.fields_by_name.get(prop_name)
                if field_desc is None:
                    errors.append(f"Unknown field: {key}")
                    continue
                
                current_val = getattr(target_obj, prop_name, None)
                is_repeated = getattr(field_desc, 'label', 0) == 3 or "Repeated" in type(current_val).__name__
                if is_repeated:
                    errors.append(f"Skipped repeated field: {key}")
                    continue
                coerced = _nc_coerce(field_desc, value, prop_name)
                if getattr(target_obj, prop_name) != coerced:
                    setattr(target_obj, prop_name, coerced)
                    if root == "localConfig":
                        sections_local.add(section)
                    else:
                        sections_module.add(section)
            elif root == "channels":
                ch_index = int(parts[1])
                target_obj = node.channels[ch_index].settings
                for part in parts[2:-1]:
                    target_obj = getattr(target_obj, part)
                prop_name = parts[-1]
                field_desc = target_obj.DESCRIPTOR.fields_by_name.get(prop_name)
                if field_desc is None:
                    errors.append(f"Unknown channel field: {key}")
                    continue
                coerced = _nc_coerce(field_desc, value, prop_name)
                if getattr(target_obj, prop_name) != coerced:
                    setattr(target_obj, prop_name, coerced)
                    channels_written.add(ch_index)
        except Exception as e:
            errors.append(f"Error applying {key}: {e}")
            continue
    if identity_change:
        ln = identity_change.get("long_name")
        sn = identity_change.get("short_name")
        if ln or sn:
            node.setOwner(long_name=ln, short_name=sn)
    written = []
    for sec in sections_local:
        try:
            node.writeConfig(sec)
            written.append(f"localConfig.{sec}")
        except Exception as e:
            errors.append(f"writeConfig({sec}) failed: {e}")
    for sec in sections_module:
        try:
            node.writeConfig(sec)
            written.append(f"moduleConfig.{sec}")
        except Exception as e:
            errors.append(f"writeConfig({sec}) failed: {e}")
    for ch_i in channels_written:
        try:
            node.writeChannel(ch_i)
            written.append(f"channel.{ch_i}")
        except Exception as e:
            errors.append(f"writeChannel({ch_i}) failed: {e}")
    return {"written": written, "errors": errors}
