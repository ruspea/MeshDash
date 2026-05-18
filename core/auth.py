# Auto-extracted from meshtastic_dashboard.py
import core.globals as g
import asyncio
import base64
import json
import logging
import secrets
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from passlib.context import CryptContext
from pydantic import BaseModel as PydanticBaseModel

from core.routes.schemas import User

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
CSRF_TOKEN_BYTES = 32
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

try:
    import pyotp
    import qrcode
    _HAS_TOTP = True
except ImportError:
    _HAS_TOTP = False
    pyotp = None
    qrcode = None

try:
    from jwt import PyJWTError as JWTError
except ImportError:
    from jwt.exceptions import InvalidTokenError as JWTError

PYDANTIC_V2 = hasattr(PydanticBaseModel, "model_dump")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta else timedelta(minutes=g.AUTH_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, g.AUTH_SECRET_KEY, algorithm=ALGORITHM)


def create_preauth_token(username: str) -> str:
    """Short-lived token for the MFA verification step (90 seconds)."""
    return jwt.encode(
        {"sub": username, "preauth": True, "exp": datetime.now(timezone.utc) + timedelta(seconds=90),
         "iat": datetime.now(timezone.utc)},
        g.AUTH_SECRET_KEY, algorithm=ALGORITHM,
    )


def verify_preauth_token(token: str) -> Optional[str]:
    """Validate a pre-auth token and return the username, or None."""
    try:
        payload = jwt.decode(token, g.AUTH_SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("preauth"):
            return None
        return payload.get("sub")
    except JWTError:
        return None


def generate_backup_codes(count: int = 8) -> Tuple[List[str], str]:
    """Generate plaintext backup codes and return (plaintext_list, hashed_json)."""
    codes = [secrets.token_hex(4).upper() for _ in range(count)]
    hashed = [pwd_context.hash(c) for c in codes]
    return codes, json.dumps(hashed)


def verify_totp_code(secret: str, code: str) -> bool:
    """Validate a TOTP code with 1 window for clock drift (RFC 6238)."""
    if not _HAS_TOTP or not pyotp:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def verify_backup_code(stored_hashes_json: str, code: str) -> Tuple[bool, Optional[str]]:
    """Check a backup code against stored hashes. Returns (valid, updated_json_or_None)."""
    try:
        hashes = json.loads(stored_hashes_json)
    except (json.JSONDecodeError, TypeError):
        return False, None
    for i, h in enumerate(hashes):
        if pwd_context.verify(code.strip().upper(), h):
            hashes.pop(i)
            return True, json.dumps(hashes)
    return False, None


def ensure_serializable(obj: Any) -> Any:
    """Convert any object to JSON-serializable format, handling protobuf objects."""
    if obj is None:
        return None
    if isinstance(obj, PydanticBaseModel):
        return obj.model_dump(mode="json") if PYDANTIC_V2 else obj.dict()
    if hasattr(obj, "DESCRIPTOR") and hasattr(obj, "ListFields"):
        return {field.name: ensure_serializable(value) for field, value in obj.ListFields()}
    if isinstance(obj, dict):
        return {k: ensure_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, deque, set)):
        return [ensure_serializable(item) for item in obj]
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return f"base64:{base64.b64encode(obj).decode('utf-8')}"
    if isinstance(obj, (int, float, bool, str)):
        return obj
    if isinstance(obj, (datetime,)):
        return str(obj)
    if hasattr(obj, "_pb"):
        return str(obj)
    try:
        return str(obj)
    except Exception:
        return None


async def get_current_active_user(request: Request) -> User:
    if g.PUBLIC_MODE:
        return User(username="public", disabled=False)
    token = request.cookies.get("access_token")
    if not token or not token.startswith("Bearer "):
        return RedirectResponse("/login", status_code=302)
    try:
        payload = jwt.decode(token.split(" ")[1], g.AUTH_SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username == "__c2_bridge__" and payload.get("internal"):
            return User(username="__c2_bridge__", disabled=False)
        if not username:
            raise JWTError("No username")
    except Exception:
        return RedirectResponse("/login", status_code=302)
    user = await asyncio.to_thread(g.db_manager.get_user, username)
    if not user or user["disabled"]:
        return RedirectResponse("/login", status_code=302)
    return User(**user)


def _generate_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


async def verify_csrf(request: Request, user: User = Depends(get_current_active_user)):
    """Validate CSRF token on state-changing requests (double-submit cookie pattern)."""
    if isinstance(user, RedirectResponse):
        return user
    cookie_token = request.cookies.get("csrf-token", "")
    header_token = request.headers.get("x-csrf-token", "")
    if not cookie_token or not header_token:
        return JSONResponse({"error": "CSRF token missing"}, status_code=403)
    if cookie_token != header_token:
        return JSONResponse({"error": "CSRF token mismatch"}, status_code=403)
    return user