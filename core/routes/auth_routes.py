"""
Authentication and user management routes.
Extracted from meshtastic_dashboard.py
"""
import io
import json
import core.globals as g
from typing import Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from core.auth import (
    User,
    create_access_token,
    create_preauth_token,
    generate_backup_codes,
    get_current_active_user,
    verify_backup_code,
    verify_password,
    verify_preauth_token,
    verify_totp_code,
)

try:
    import pyotp
    import qrcode
    _HAS_TOTP = True
except ImportError:
    _HAS_TOTP = False
    pyotp = None
    qrcode = None

router = APIRouter(tags=["auth"])

# Lazy-loaded globals (avoid circular imports at module load time)
def _get_globals():
    from meshtastic_dashboard import db_manager, PUBLIC_MODE, LOGIN_HTML_PATH
    from meshtastic_dashboard import STATIC_DIR, AUTH_SECRET_KEY, AUTH_TOKEN_EXPIRE_MINUTES
    return db_manager, PUBLIC_MODE, LOGIN_HTML_PATH, STATIC_DIR, AUTH_SECRET_KEY, AUTH_TOKEN_EXPIRE_MINUTES


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(error: str = None):
    db_manager, PUBLIC_MODE, LOGIN_HTML_PATH, STATIC_DIR, _, _ = _get_globals()
    if PUBLIC_MODE:
        return RedirectResponse("/", 302)
    import asyncio
    count = await asyncio.to_thread(db_manager.count_users)
    if count == 0:
        import logging
        logging.getLogger("meshtastic_dashboard").info("🔧 Login accessed but no users exist - serving login page")
    if LOGIN_HTML_PATH and __import__('os').path.exists(LOGIN_HTML_PATH):
        with open(LOGIN_HTML_PATH) as f:
            content = f.read()
        if error:
            content = content.replace(
                '<div id="message-container"',
                f'<div id="message-container"><div class="message-box message-box-error"><strong>Error:</strong><p>{error}</p></div>',
            )
        return HTMLResponse(content)
    raise HTTPException(404, "Login page not found")


@router.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    db_manager, PUBLIC_MODE, _, _, _, _ = _get_globals()
    if PUBLIC_MODE:
        return RedirectResponse("/", 302)
    import asyncio
    user = await asyncio.to_thread(db_manager.get_user, username)
    if not user or not verify_password(password, user["hashed_password"]):
        return RedirectResponse("/login?error=Invalid+Credentials", 302)

    if user.get("disabled"):
        return RedirectResponse("/login?error=Account+suspended.+Contact+your+administrator.", 302)

    if user.get("totp_enabled"):
        preauth = create_preauth_token(user["username"])
        return JSONResponse({"mfa_required": True, "preauth_token": preauth})

    if user.get("must_setup_mfa") or (user.get("force_mfa") and not user.get("totp_enabled")):
        preauth = create_preauth_token(user["username"])
        return JSONResponse({"mfa_setup_required": True, "preauth_token": preauth})

    await asyncio.to_thread(db_manager.record_login, user["username"])
    resp = RedirectResponse("/", 302)
    resp.set_cookie(
        "access_token",
        f"Bearer {create_access_token({'sub': user['username']})}",
        httponly=True,
        samesite="strict",
    )
    return resp


@router.post("/login/verify-totp")
async def login_verify_totp(
    preauth_token: str = Form(...),
    totp_code: str = Form(...),
):
    db_manager, PUBLIC_MODE, _, _, _, _ = _get_globals()
    if PUBLIC_MODE:
        return RedirectResponse("/", 302)

    username = verify_preauth_token(preauth_token)
    if not username:
        return JSONResponse({"error": "Session expired. Please log in again."}, status_code=401)

    import asyncio
    user = await asyncio.to_thread(db_manager.get_user, username)
    if not user or not user.get("totp_enabled") or not user.get("totp_secret"):
        return JSONResponse({"error": "MFA configuration error."}, status_code=400)

    code = totp_code.strip().replace(" ", "")
    valid = False

    if code.isdigit() and len(code) == 6:
        valid = verify_totp_code(user["totp_secret"], code)

    if not valid and user.get("backup_codes"):
        bc_valid, updated_json = verify_backup_code(user["backup_codes"], code)
        if bc_valid:
            valid = True
            await asyncio.to_thread(db_manager.consume_backup_code, username, updated_json)

    if not valid:
        return JSONResponse({"error": "Invalid verification code."}, status_code=401)

    await asyncio.to_thread(db_manager.record_login, username)
    token = create_access_token({"sub": username})
    resp = JSONResponse({"success": True, "redirect": "/"})
    resp.set_cookie("access_token", f"Bearer {token}", httponly=True, samesite="strict")
    return resp


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# ---------------------------------------------------------------------------
# TOTP Status
# ---------------------------------------------------------------------------

@router.get("/api/totp/status")
async def totp_status(user: User = Depends(get_current_active_user)):
    if isinstance(user, RedirectResponse):
        return user
    db_manager, _, _, _, _, _ = _get_globals()
    import asyncio
    db_user = await asyncio.to_thread(db_manager.get_user, user.username)
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


# ---------------------------------------------------------------------------
# TOTP Setup
# ---------------------------------------------------------------------------

@router.post("/api/totp/setup")
async def totp_setup(user: User = Depends(get_current_active_user)):
    if isinstance(user, RedirectResponse):
        return user
    if not _HAS_TOTP:
        raise HTTPException(501, "TOTP libraries not installed (pip install pyotp qrcode[pil])")

    db_manager, _, _, _, _, _ = _get_globals()
    import asyncio

    secret = pyotp.random_base32()
    await asyncio.to_thread(db_manager.set_totp_secret, user.username, secret)

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=user.username, issuer_name="MeshDash")

    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = __import__('base64').b64encode(buf.getvalue()).decode()

    return {
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_b64}",
        "provisioning_uri": provisioning_uri,
    }


@router.post("/api/totp/confirm")
async def totp_confirm(
    code: str = Form(...),
    user: User = Depends(get_current_active_user),
):
    if isinstance(user, RedirectResponse):
        return user
    if not _HAS_TOTP:
        raise HTTPException(501, "TOTP libraries not installed")

    db_manager, _, _, _, _, _ = _get_globals()
    import asyncio

    db_user = await asyncio.to_thread(db_manager.get_user, user.username)
    if not db_user or not db_user.get("totp_secret"):
        raise HTTPException(400, "Run /api/totp/setup first")

    if not verify_totp_code(db_user["totp_secret"], code.strip()):
        raise HTTPException(401, "Invalid code. Check your authenticator and try again.")

    plaintext_codes, hashed_json = generate_backup_codes()
    await asyncio.to_thread(db_manager.enable_totp, user.username, hashed_json)

    return {
        "enabled": True,
        "backup_codes": plaintext_codes,
        "message": "MFA enabled. Save these backup codes — they will not be shown again.",
    }


@router.post("/api/totp/disable")
async def totp_disable(
    password: str = Form(...),
    user: User = Depends(get_current_active_user),
):
    if isinstance(user, RedirectResponse):
        return user

    db_manager, _, _, _, _, _ = _get_globals()
    import asyncio

    db_user = await asyncio.to_thread(db_manager.get_user, user.username)
    if not db_user or not verify_password(password, db_user["hashed_password"]):
        raise HTTPException(401, "Invalid password")

    await asyncio.to_thread(db_manager.disable_totp, user.username)
    return {"enabled": False, "message": "MFA has been disabled."}


# ---------------------------------------------------------------------------
# Forced MFA Setup (during login, before session is granted)
# ---------------------------------------------------------------------------

@router.post("/login/setup-totp")
async def login_setup_totp(preauth_token: str = Form(...)):
    if not _HAS_TOTP:
        return JSONResponse({"error": "TOTP libraries not installed on server."}, status_code=501)

    username = verify_preauth_token(preauth_token)
    if not username:
        return JSONResponse({"error": "Session expired. Please log in again."}, status_code=401)

    db_manager, _, _, _, _, _ = _get_globals()
    import asyncio

    secret = pyotp.random_base32()
    await asyncio.to_thread(db_manager.set_totp_secret, username, secret)

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=username, issuer_name="MeshDash")
    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = __import__('base64').b64encode(buf.getvalue()).decode()

    return JSONResponse({
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_b64}",
    })


@router.post("/login/confirm-totp-setup")
async def login_confirm_totp_setup(preauth_token: str = Form(...), totp_code: str = Form(...)):
    username = verify_preauth_token(preauth_token)
    if not username:
        return JSONResponse({"error": "Session expired. Please log in again."}, status_code=401)

    db_manager, _, _, _, _, _ = _get_globals()
    import asyncio

    user = await asyncio.to_thread(db_manager.get_user, username)
    if not user or not user.get("totp_secret"):
        return JSONResponse({"error": "MFA setup not started."}, status_code=400)

    if not verify_totp_code(user["totp_secret"], totp_code.strip()):
        return JSONResponse({"error": "Invalid code. Check your authenticator and try again."}, status_code=401)

    plaintext_codes, hashed_json = generate_backup_codes()
    await asyncio.to_thread(db_manager.enable_totp, username, hashed_json)
    await asyncio.to_thread(db_manager.clear_must_setup_mfa, username)
    await asyncio.to_thread(db_manager.record_login, username)

    token = create_access_token({"sub": username})
    resp = JSONResponse({
        "success": True,
        "redirect": "/",
        "backup_codes": plaintext_codes,
        "message": "MFA enabled successfully. Save these backup codes — they will not be shown again.",
    })
    resp.set_cookie("access_token", f"Bearer {token}", httponly=True, samesite="strict")
    return resp


# ---------------------------------------------------------------------------
# Account / User Info
# ---------------------------------------------------------------------------

@router.get("/api/account/me")
async def account_me(user: User = Depends(get_current_active_user)):
    if isinstance(user, RedirectResponse):
        return user

    db_manager, _, _, _, _, _ = _get_globals()
    import asyncio

    db_user = await asyncio.to_thread(db_manager.get_user, user.username)
    if not db_user:
        raise HTTPException(404, "User not found.")
    backup_remaining = 0
    if db_user.get("backup_codes"):
        try:
            backup_remaining = len(json.loads(db_user["backup_codes"]))
        except Exception:
            pass

    ROLE_LABELS = {0: "Admin", 1: "Operator", 2: "Spectator"}
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



