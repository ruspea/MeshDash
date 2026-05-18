import core.globals as g
# Auto-extracted from meshtastic_dashboard.py
import asyncio
import logging
import os
from typing import Optional
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from core.auth import (verify_password, create_access_token, create_preauth_token,
                       verify_preauth_token, verify_totp_code, generate_backup_codes,
                       verify_backup_code, _generate_csrf_token)
from core.middleware import _check_login_not_locked, _record_login_failure, _clear_login_failure

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    error: str = None,
    username: str = None,
    password: str = None,
    totp_code: str = None,
):
    # SECURITY: If credentials were accidentally put in the URL (e.g. via bookmark
    # or browser autofill), redirect to clean login page immediately.
    # Credentials should NEVER be in URLs  they get logged by proxies and
    # browser history.
    if username or password or totp_code:
        return RedirectResponse("/login", status_code=307)
    
    if g.PUBLIC_MODE:
        return RedirectResponse("/", 302)
    count = await asyncio.to_thread(g.db_manager.count_users)
    if count == 0:
        logger.info("? Login accessed but no users exist - serving login page")
    if os.path.exists(g.LOGIN_HTML_PATH):
        with open(g.LOGIN_HTML_PATH) as f:
            content = f.read()
        if error:
            content = content.replace(
                '<div id="message-container"',
                f'<div id="message-container"><div class="message-box message-box-error"><strong>Error:</strong><p>{error}</p></div>',
            )
        return HTMLResponse(content)
    raise HTTPException(404, "Login page not found")


@router.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    logger.info(f"LOGIN POST: username={username}, content_type={request.headers.get('content-type')}")
    if g.PUBLIC_MODE:
        return RedirectResponse("/", 302)
    # Brute-force protection
    if not _check_login_not_locked(username) is None:
        msg = _check_login_not_locked(username)
        return RedirectResponse(f"/login?error={msg.replace(' ', '+')}", 302)
    user = await asyncio.to_thread(g.db_manager.get_user, username)
    if not user:
        _record_login_failure(username)
        logger.warning(f"LOGIN FAIL: user '{username}' not found")
        return RedirectResponse("/login?error=Invalid+Credentials", 302)
    if not verify_password(password, user["hashed_password"]):
        _record_login_failure(username)
        logger.warning(f"LOGIN FAIL: wrong password for '{username}'")
        return RedirectResponse("/login?error=Invalid+Credentials", 302)

    # Suspended account
    if user.get("disabled"):
        _record_login_failure(username)
        return RedirectResponse("/login?error=Account+suspended.+Contact+your+administrator.", 302)

    # --- MFA gate: if TOTP enabled, return pre-auth token for verification ---
    if user.get("totp_enabled"):
        _clear_login_failure(username)
        preauth = create_preauth_token(user["username"])
        return JSONResponse({"mfa_required": True, "preauth_token": preauth})

    # --- Forced MFA setup: user must configure TOTP before accessing dashboard ---
    if user.get("must_setup_mfa") or (user.get("force_mfa") and not user.get("totp_enabled")):
        _clear_login_failure(username)
        preauth = create_preauth_token(user["username"])
        return JSONResponse({"mfa_setup_required": True, "preauth_token": preauth})

    logger.info(f"LOGIN SUCCESS: '{username}'  no MFA, setting cookie")
    _clear_login_failure(username)
    await asyncio.to_thread(g.db_manager.record_login, user["username"])
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
    """Phase 2 of MFA login: validate the TOTP code (or backup code) with the pre-auth token."""
    if g.PUBLIC_MODE:
        return RedirectResponse("/", 302)

    username = verify_preauth_token(preauth_token)
    if not username:
        return JSONResponse({"error": "Session expired. Please log in again."}, status_code=401)

    user = await asyncio.to_thread(g.db_manager.get_user, username)
    if not user or not user.get("totp_enabled") or not user.get("totp_secret"):
        return JSONResponse({"error": "MFA configuration error."}, status_code=400)

    code = totp_code.strip().replace(" ", "")
    valid = False

    # Try TOTP first (6-digit numeric)
    if code.isdigit() and len(code) == 6:
        valid = verify_totp_code(user["totp_secret"], code)

    # Fall back to backup code (8-char hex)
    if not valid and user.get("backup_codes"):
        bc_valid, updated_json = verify_backup_code(user["backup_codes"], code)
        if bc_valid:
            valid = True
            await asyncio.to_thread(g.db_manager.consume_backup_code, username, updated_json)

    if not valid:
        return JSONResponse({"error": "Invalid verification code."}, status_code=401)

    await asyncio.to_thread(g.db_manager.record_login, username)
    token = create_access_token({"sub": username})
    resp = JSONResponse({"success": True, "redirect": "/"})
    resp.set_cookie("csrf-token", _generate_csrf_token(), path="/", same_site="strict", httponly=True)
    resp.set_cookie("access_token", f"Bearer {token}", httponly=True, samesite="strict")
    return resp


@router.post("/login/confirm-totp-setup")
async def login_confirm_totp_setup(preauth_token: str = Form(...), totp_code: str = Form(...)):
    """Confirm first TOTP code during forced MFA setup, then issue session."""
    username = verify_preauth_token(preauth_token)
    if not username:
        return JSONResponse({"error": "Session expired. Please log in again."}, status_code=401)

    user = await asyncio.to_thread(g.db_manager.get_user, username)
    if not user or not user.get("totp_secret"):
        return JSONResponse({"error": "MFA setup not started."}, status_code=400)

    if not verify_totp_code(user["totp_secret"], totp_code.strip()):
        return JSONResponse({"error": "Invalid code. Check your authenticator and try again."}, status_code=401)

    plaintext_codes, hashed_json = generate_backup_codes()
    await asyncio.to_thread(g.db_manager.enable_totp, username, hashed_json)
    await asyncio.to_thread(g.db_manager.clear_must_setup_mfa, username)
    await asyncio.to_thread(g.db_manager.record_login, username)

    token = create_access_token({"sub": username})
    resp = JSONResponse({
        "success": True,
        "redirect": "/",
        "backup_codes": plaintext_codes,
        "message": "MFA enabled successfully. Save these backup codes  they will not be shown again.",
    })
    resp.set_cookie("csrf-token", _generate_csrf_token(), path="/", same_site="strict", httponly=True)
    resp.set_cookie("access_token", f"Bearer {token}", httponly=True, samesite="strict")
    return resp


@router.get("/logout")
async def logout():
    r = RedirectResponse("/login", 302)
    r.delete_cookie("access_token")
    return r


@router.get("/favicon.ico")
async def favicon():
    if os.path.exists(g.FAVICON_PATH):
        return FileResponse(g.FAVICON_PATH)
    raise HTTPException(404, "Favicon not found")


