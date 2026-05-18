from core.c2 import send_system_message
from core.config_loader import load_configuration, _save_slots_file
import core.globals as g
# Auto-extracted from meshtastic_dashboard.py
import asyncio
import base64
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, Request, Depends, HTTPException, Form, File, UploadFile, status
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from core.routes.schemas import User, ConfigUpdateRequest, SetupWizardPayload
from core.auth import verify_csrf, get_current_active_user, get_password_hash, create_access_token, _generate_csrf_token, ensure_serializable, PYDANTIC_V2
from core.middleware import _require_admin

ROLE_LABELS = {0: "Admin", 1: "Operator", 2: "Spectator"}

# TOTP support — try to import optional dependencies
try:
    import pyotp
    import qrcode
    _HAS_TOTP = True
except ImportError:
    _HAS_TOTP = False
    pyotp = None
    qrcode = None

logger = logging.getLogger(__name__)
router = APIRouter()
@router.get("/api/totp/status")
async def totp_status(user: User = Depends(get_current_active_user)):
    """Check if TOTP is available and enabled for the current user."""
    if isinstance(user, RedirectResponse):
        return user
    db_user = await asyncio.to_thread(g.db_manager.get_user, user.username)
    remaining_codes = 0
    if db_user and db_user.get("backup_codes"):
        try:
            remaining_codes = len(json.loads(db_user["backup_codes"]))
        except Exception:
            pass
    return {
        "totp_available": _HAS_TOTP,
        "totp_enabled": bool(db_user and db_user.get("totp_enabled")),
        "backup_codes_remaining": remaining_codes,
    }


@router.post("/api/totp/setup")
async def totp_setup(user: User = Depends(verify_csrf)):
    """Generate a new TOTP secret and return a QR code (base64 PNG) + manual key."""
    if isinstance(user, RedirectResponse):
        return user
    if not _HAS_TOTP:
        raise HTTPException(501, "TOTP libraries not installed (pip install pyotp qrcode[pil])")

    secret = pyotp.random_base32()
    await asyncio.to_thread(g.db_manager.set_totp_secret, user.username, secret)

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=user.username, issuer_name="MeshDash")

    # Generate QR code as base64 PNG
    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return {
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_b64}",
        "provisioning_uri": provisioning_uri,
    }


@router.post("/api/totp/confirm")
async def totp_confirm(
    code: str = Form(...),
    user: User = Depends(verify_csrf),
):
    """Verify the first TOTP code to activate MFA. Returns one-time backup codes."""
    if isinstance(user, RedirectResponse):
        return user
    if not _HAS_TOTP:
        raise HTTPException(501, "TOTP libraries not installed")

    db_user = await asyncio.to_thread(g.db_manager.get_user, user.username)
    if not db_user or not db_user.get("totp_secret"):
        raise HTTPException(400, "Run /api/totp/setup first")

    if not verify_totp_code(db_user["totp_secret"], code.strip()):
        raise HTTPException(401, "Invalid code. Check your authenticator and try again.")

    plaintext_codes, hashed_json = generate_backup_codes()
    await asyncio.to_thread(g.db_manager.enable_totp, user.username, hashed_json)

    return {
        "enabled": True,
        "backup_codes": plaintext_codes,
        "message": "MFA enabled. Save these backup codes  they will not be shown again.",
    }


@router.post("/api/totp/disable")
async def totp_disable(
    password: str = Form(...),
    user: User = Depends(verify_csrf),
):
    """Disable TOTP MFA (requires password confirmation)."""
    if isinstance(user, RedirectResponse):
        return user

    db_user = await asyncio.to_thread(g.db_manager.get_user, user.username)
    if not db_user or not verify_password(password, db_user["hashed_password"]):
        raise HTTPException(401, "Invalid password")

    await asyncio.to_thread(g.db_manager.disable_totp, user.username)
    return {"enabled": False, "message": "MFA has been disabled."}


@router.post("/login/setup-totp")
async def login_setup_totp(preauth_token: str = Form(...)):
    """Generate TOTP secret + QR for a user who must set up MFA before accessing the dashboard."""
    if not _HAS_TOTP:
        return JSONResponse({"error": "TOTP libraries not installed on server."}, status_code=501)
    username = verify_preauth_token(preauth_token)
    if not username:
        return JSONResponse({"error": "Session expired. Please log in again."}, status_code=401)

    secret = pyotp.random_base32()
    await asyncio.to_thread(g.db_manager.set_totp_secret, username, secret)

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=username, issuer_name="MeshDash")
    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return JSONResponse({
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_b64}",
    })


@router.get("/api/account/me")
async def account_me(user: User = Depends(get_current_active_user)):
    """Return full profile for the currently logged-in user."""
    if isinstance(user, RedirectResponse):
        return user
    db_user = await asyncio.to_thread(g.db_manager.get_user, user.username)
    if not db_user:
        raise HTTPException(404, "User not found.")
    backup_remaining = 0
    if db_user.get("backup_codes"):
        try:
            backup_remaining = len(json.loads(db_user["backup_codes"]))
        except Exception:
            pass
    return {
        "username": db_user["username"],
        "role": db_user.get("role", 1),
        "role_label": ROLE_LABELS.get(db_user.get("role", 1), "Unknown"),
        "disabled": bool(db_user.get("disabled")),
        "totp_enabled": bool(db_user.get("totp_enabled")),
        "totp_available": _HAS_TOTP,
        "force_mfa": bool(db_user.get("force_mfa")),
        "backup_codes_remaining": backup_remaining,
        "last_login": db_user.get("last_login"),
        "login_count": db_user.get("login_count", 0),
        "created_at": db_user.get("created_at"),
    }


@router.get("/api/users")
async def list_users(user: User = Depends(get_current_active_user)):
    _require_admin(user)
    users = await asyncio.to_thread(g.db_manager.get_all_users_full)
    for u in users:
        u["role_label"] = ROLE_LABELS.get(u.get("role", 1), "Unknown")
    return {"users": users}


@router.post("/api/users")
async def create_user_api(request: Request, user: User = Depends(verify_csrf)):
    _require_admin(user)
    body = await request.json()
    uname = body.get("username", "").strip()
    password = body.get("password", "").strip()
    role = int(body.get("role", 1))
    force_mfa = bool(body.get("force_mfa", False))

    if not uname or len(uname) < 2:
        raise HTTPException(400, "Username must be at least 2 characters.")
    if not password or len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")
    if role not in (0, 1, 2):
        raise HTTPException(400, "Invalid role. Must be 0 (Admin), 1 (Operator), or 2 (Spectator).")

    hashed = await asyncio.to_thread(get_password_hash, password)
    result = await asyncio.to_thread(
        g.db_manager.create_user, uname, hashed, role=role,
        force_mfa=force_mfa, must_setup_mfa=force_mfa,
    )
    if not result:
        raise HTTPException(409, f"Username '{uname}' already exists.")

    logger.info(f"? User '{uname}' created by admin '{user.username}' (role={role}, force_mfa={force_mfa})")
    return {"status": "created", "username": uname, "role": role, "role_label": ROLE_LABELS.get(role)}


@router.put("/api/users/{target_username}/role")
async def update_user_role_api(target_username: str, request: Request, user: User = Depends(verify_csrf)):
    _require_admin(user)
    body = await request.json()
    role = int(body.get("role", 1))
    if role not in (0, 1, 2):
        raise HTTPException(400, "Invalid role.")

    target = await asyncio.to_thread(g.db_manager.get_user, target_username)
    if not target:
        raise HTTPException(404, "User not found.")

    await asyncio.to_thread(g.db_manager.update_user_role, target_username, role)
    logger.info(f"? User '{target_username}' role changed to {role} by '{user.username}'")
    return {"status": "updated", "username": target_username, "role": role, "role_label": ROLE_LABELS.get(role)}


@router.put("/api/users/{target_username}/password")
async def reset_user_password_api(target_username: str, request: Request, user: User = Depends(verify_csrf)):
    _require_admin(user)
    body = await request.json()
    password = body.get("password", "").strip()
    if not password or len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")

    target = await asyncio.to_thread(g.db_manager.get_user, target_username)
    if not target:
        raise HTTPException(404, "User not found.")

    hashed = await asyncio.to_thread(get_password_hash, password)
    await asyncio.to_thread(g.db_manager.update_user_password, target_username, hashed)
    logger.info(f"? Password reset for '{target_username}' by admin '{user.username}'")
    return {"status": "password_reset", "username": target_username}


@router.put("/api/users/{target_username}/suspend")
async def suspend_user_api(target_username: str, request: Request, user: User = Depends(verify_csrf)):
    _require_admin(user)
    body = await request.json()
    suspended = bool(body.get("suspended", True))

    target = await asyncio.to_thread(g.db_manager.get_user, target_username)
    if not target:
        raise HTTPException(404, "User not found.")
    if target_username == user.username:
        raise HTTPException(400, "You cannot suspend your own account.")

    await asyncio.to_thread(g.db_manager.suspend_user, target_username, suspended)
    action = "suspended" if suspended else "reactivated"
    logger.info(f"? User '{target_username}' {action} by admin '{user.username}'")
    return {"status": action, "username": target_username}


@router.put("/api/users/{target_username}/force-mfa")
async def force_mfa_api(target_username: str, request: Request, user: User = Depends(verify_csrf)):
    _require_admin(user)
    body = await request.json()
    force = bool(body.get("force_mfa", True))

    target = await asyncio.to_thread(g.db_manager.get_user, target_username)
    if not target:
        raise HTTPException(404, "User not found.")

    must_setup = force and not target.get("totp_enabled")
    await asyncio.to_thread(g.db_manager.set_force_mfa, target_username, force, must_setup)
    logger.info(f"? MFA {'enforced' if force else 'unenforced'} for '{target_username}' by '{user.username}'")
    return {"status": "updated", "force_mfa": force, "must_setup_mfa": must_setup}


@router.delete("/api/users/{target_username}/mfa")
async def admin_reset_mfa_api(target_username: str, user: User = Depends(verify_csrf)):
    """Admin resets another user's MFA (removes their TOTP). If force_mfa is on, they'll be prompted to set up again."""
    _require_admin(user)
    target = await asyncio.to_thread(g.db_manager.get_user, target_username)
    if not target:
        raise HTTPException(404, "User not found.")

    await asyncio.to_thread(g.db_manager.disable_totp, target_username)
    if target.get("force_mfa"):
        await asyncio.to_thread(g.db_manager.set_force_mfa, target_username, True, True)
    logger.info(f"? MFA reset for '{target_username}' by admin '{user.username}'")
    return {"status": "mfa_reset", "username": target_username}


@router.delete("/api/users/{target_username}")
async def delete_user_api(target_username: str, user: User = Depends(verify_csrf)):
    _require_admin(user)
    if target_username == user.username:
        raise HTTPException(400, "You cannot delete your own account.")

    target = await asyncio.to_thread(g.db_manager.get_user, target_username)
    if not target:
        raise HTTPException(404, "User not found.")

    deleted = await asyncio.to_thread(g.db_manager.delete_user, target_username)
    if not deleted:
        raise HTTPException(500, "Delete failed.")
    logger.info(f"??  User '{target_username}' deleted by admin '{user.username}'")
    return {"status": "deleted", "username": target_username}


@router.post("/api/users/generate-password")
async def generate_password_api(user: User = Depends(verify_csrf)):
    """Generate a cryptographically secure random password."""
    _require_admin(user)
    pw = secrets.token_urlsafe(16)
    return {"password": pw}


# ─────────────────────────────────────────────────────────────────────────────
# Setup flag helpers
# ─────────────────────────────────────────────────────────────────────────────

_SETUP_FLAG_FILES = ["setup.flag", ".setup", ".new"]


def _cleanup_setup_flags():
    """Remove all setup indicator files from both DATA_DIR and STATIC_DIR.

    Consolidated single source of truth. Every code path that needs to
    clean up setup flags calls this function.
    """
    for flag_name in _SETUP_FLAG_FILES:
        for flag_dir in [g.DATA_DIR, g.STATIC_DIR]:
            fp = os.path.join(flag_dir, flag_name)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass


def _create_setup_flags():
    """Create setup indicator files ONLY when the system is genuinely uninitialised
    AND was NOT provisioned via C2 setup wizard.

    Called at startup. Checks:
      1. c2_installed.flag — if present, skip entirely (C2-provisioned)
      2. Database — if users exist, skip
      3. Otherwise, write setup flags for manual install
    """
    # R3.0+: Check for C2 installation flag first
    c2_flag_path = os.path.join(g.DATA_DIR, "c2_installed.flag")
    if os.path.exists(c2_flag_path):
        # C2-provisioned install — do NOT show setup wizard
        # The config already has INITIAL_ADMIN_USERNAME/PASSWORD
        logger.info("🦀 C2 installation flag detected — skipping setup wizard")
        return

    try:
        import sqlite3 as _sqlite
        conn = _sqlite.connect(g.DATA_DIR + "/meshtastic_data.db" if not g.DB_PATH else g.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
        if count == 0:
            for flag_name in ["setup.flag", ".setup"]:
                flag_path = os.path.join(g.DATA_DIR, flag_name)
                try:
                    with open(flag_path, "w") as f:
                        f.write(str(time.time()))
                except Exception:
                    pass
    except Exception:
        # DB may not exist yet (fresh install) — create flags unconditionally
        for flag_name in ["setup.flag", ".setup"]:
            flag_path = os.path.join(g.DATA_DIR, flag_name)
            try:
                with open(flag_path, "w") as f:
                    f.write(str(time.time()))
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Initial Setup — POST /api/system/config/initial-setup
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/system/config/initial-setup")
async def initial_setup_api(payload: SetupWizardPayload):
    """Process setup wizard submission.

    Validated with Pydantic (SetupWizardPayload). Uses atomic user creation
    to eliminate the race window between count_users and create_user.
    """
    username = payload.adminUser.username.strip()
    password = payload.adminUser.password
    config_values = payload.configValues

    if not username or not password:
        raise HTTPException(400, "Username and password required")

    logger.info(f"🔐 Starting initial setup for admin user: {username}")

    # ── Step 1: Atomic user creation (count + insert in one transaction) ──
    hashed = await asyncio.to_thread(get_password_hash, password)
    result = await asyncio.to_thread(
        g.db_manager.atomic_setup_user, username, hashed
    )
    if result is None:
        logger.warning(f"🔒 Setup blocked — users already exist")
        _cleanup_setup_flags()
        raise HTTPException(
            400,
            "System already initialized. Please log in or see an administrator.",
        )

    logger.info(f"✅ Admin user '{username}' created successfully")

    # ── Step 2: Write config file ──
    try:
        existing_config = {}
        if os.path.exists(g.CONFIG_FILE_PATH):
            try:
                with open(g.CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            existing_config[k.strip()] = v.strip()
            except Exception:
                pass

        config_data = dict(existing_config)
        config_data.update(config_values)
        for key in ("adminUser", "configValues", "rawSelections", "username", "password"):
            config_data.pop(key, None)

        def _write_initial():
            with open(g.CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
                f.write("# ---------------------------------------------------------\n")
                f.write("# MeshDash Configuration\n")
                f.write(f"# Generated via Web Setup on {datetime.now().isoformat()}\n")
                f.write("# ---------------------------------------------------------\n\n")
                for key, val in config_data.items():
                    if isinstance(val, dict):
                        continue
                    val_str = str(val) if val is not None else ""
                    f.write(f"{key}={val_str}\n")

        await asyncio.to_thread(_write_initial)
        logger.info(f"✅ Configuration written to {g.CONFIG_FILE_PATH}")

    except PermissionError as e:
        logger.error(f"❌ Permission denied writing config file: {e}")
        _cleanup_setup_flags()
        raise HTTPException(
            500,
            "Config file could not be written (permission denied). "
            "Check filesystem permissions and retry setup.",
        )
    except Exception as e:
        logger.error(f"❌ Failed to write config file: {e}")
        _cleanup_setup_flags()
        raise HTTPException(
            500,
            f"Config file could not be written: {e}. "
            "Setup incomplete — please retry.",
        )

    # ── Step 3: Hot-reload ALL connection globals ──
    _auth_key = str(config_data.get("AUTH_SECRET_KEY", ""))
    _auth_expire = int(config_data.get("AUTH_TOKEN_EXPIRE_MINUTES", 10080))
    g.AUTH_SECRET_KEY = _auth_key
    g.AUTH_TOKEN_EXPIRE_MINUTES = _auth_expire

    _community_key = str(config_data.get("COMMUNITY_API_KEY", ""))
    if _community_key:
        g.COMMUNITY_API_KEY = _community_key

    # Connection globals — all five, not just host/port
    _conn_type = str(config_data.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL"))
    _host = str(config_data.get("MESHTASTIC_HOST", ""))
    _port_str = str(config_data.get("MESHTASTIC_PORT", "4403"))
    _serial = str(config_data.get("MESHTASTIC_SERIAL_PORT", ""))
    _ble = str(config_data.get("MESHTASTIC_BLE_MAC", ""))

    g.MESHTASTIC_CONNECTION_TYPE = _conn_type
    if _host:
        g.TARGET_HOST = _host
    try:
        g.TARGET_PORT = int(_port_str)
    except (ValueError, TypeError):
        g.TARGET_PORT = 4403
    g.MESHTASTIC_SERIAL_PORT = _serial
    g.MESHTASTIC_BLE_MAC = _ble

    logger.info(
        "♻️  Hot-reloaded globals: "
        f"connection_type={g.MESHTASTIC_CONNECTION_TYPE}, "
        f"host={g.TARGET_HOST}, port={g.TARGET_PORT}, "
        f"serial={g.MESHTASTIC_SERIAL_PORT}, ble={g.MESHTASTIC_BLE_MAC}"
    )

    # ── Step 4: Clean up flags, issue token ──
    _cleanup_setup_flags()

    token = create_access_token({"sub": username})
    token_expire_minutes = int(config_data.get("AUTH_TOKEN_EXPIRE_MINUTES", 10080))

    response = JSONResponse({
        "status": "success",
        "message": "Setup completed successfully",
        "username": username,
        "redirect": "/",
    })
    response.set_cookie(
        key="access_token",
        value=f"Bearer {token}",
        httponly=True,
        max_age=token_expire_minutes * 60,
        samesite="strict",
    )
    logger.info(f"🎉 Setup finalized. Redirecting '{username}' to dashboard.")
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Setup Page — GET /setup
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def setup_page():
    if g.PUBLIC_MODE:
        return RedirectResponse("/", 302)

    count = await asyncio.to_thread(g.db_manager.count_users)
    if count > 0:
        _cleanup_setup_flags()
        return RedirectResponse("/login", 302)

    setup_html_path = os.path.join(g.STATIC_DIR, "setup.html")
    if not os.path.exists(setup_html_path):
        raise HTTPException(404, "Setup page not found")
    return FileResponse(setup_html_path)

